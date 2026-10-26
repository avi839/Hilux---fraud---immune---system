"""API_Server security controls (task 11.2, Req 27.1-27.4) and latency-breach exposure (Req 25.6, 25.7).

Every app here is built with ``secure_app`` (the path ``python -m helix.api`` serves).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helix.api import AppState, check_bind, is_loopback, secure_app
from helix.api import __main__ as api_main
from helix.api.security import DEFAULT_MAX_BODY_BYTES, validate_origin
from helix.config import HelixConfig
from helix.config.schema import ApiConfig
from helix.core import UNAVAILABLE, LatencyRecorder, Stage
from helix.sim.clock import ManualClock

pytestmark = pytest.mark.integration

API = "/api/v1"
ORIGIN = "http://localhost:5173"  # ApiConfig default
EVIL = "http://evil.example"
START = datetime(2024, 6, 2, tzinfo=timezone.utc)
NO_AUTH = "no authentication or authorisation"


def make_state(cfg: HelixConfig | None = None, **kw: Any) -> AppState:
    return AppState.build(cfg if cfg is not None else HelixConfig(), ManualClock(START), learner=False, **kw)


def event_doc(tx: str = "E1") -> dict:
    return {"tx_id": tx, "ts": "2024-06-03T09:15:00Z", "src": "A-1", "dst": "B-1", "amount": "42.50",
            "currency": "EUR", "channel": "card"}


def all_header_values(r: Any) -> list[str]:
    return [v for _, v in r.headers.multi_items()]


@pytest.fixture
def state() -> AppState:
    return make_state()


@pytest.fixture
def client(state: AppState):
    with TestClient(secure_app(state, manage_lifecycle=False)) as c:
        yield c


# --- bind address (Req 27.1, 27.2) -----------------------------------------------------------


def test_default_bind_is_loopback():
    assert HelixConfig().api.bind_host == "127.0.0.1" and is_loopback("127.0.0.1")


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.1", "fe80::1", "::ffff:127.0.0.1",
                                  "example.com", "my-laptop", ""])
def test_non_loopback_bind_logs_no_auth_warning(host: str, caplog: pytest.LogCaptureFixture):
    logger = logging.getLogger("helix.api.test")
    with caplog.at_level(logging.WARNING, logger="helix.api.test"):
        assert check_bind(host, logger) is False
    [rec] = caplog.records
    assert rec.levelno == logging.WARNING
    assert repr(host) in rec.getMessage() and NO_AUTH in rec.getMessage()


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "127.255.255.255", "::1", "[::1]", "localhost",
                                  "LOCALHOST", " 127.0.0.1 "])
def test_loopback_bind_logs_nothing(host: str, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.DEBUG):
        assert check_bind(host, logging.getLogger("helix.api.test")) is True
    assert not [r for r in caplog.records if r.name == "helix.api.test"]


def test_main_warns_before_serving_and_serves_the_secure_app(monkeypatch, caplog: pytest.LogCaptureFixture):
    """``python -m helix.api --host 0.0.0.0`` logs the warning before uvicorn starts serving."""
    import uvicorn

    seen: dict[str, Any] = {}

    def fake_run(app: Any, host: str, port: int, **_: Any) -> None:
        seen["warned_before_run"] = any(NO_AUTH in r.getMessage() and "'0.0.0.0'" in r.getMessage()
                                        for r in caplog.records if r.levelno == logging.WARNING)
        seen["secure"], seen["host"], seen["port"] = app.state.secure, host, port

    monkeypatch.setattr(uvicorn, "run", fake_run)
    with caplog.at_level(logging.WARNING):
        api_main.main(["--host", "0.0.0.0", "--port", "8123", "--no-generator"])
    assert seen == {"warned_before_run": True, "secure": True, "host": "0.0.0.0", "port": 8123}


# --- CORS (Req 27.3) ------------------------------------------------------------------------------


def preflight(c: TestClient, origin: str, method: str = "GET", path: str = f"{API}/cases") -> Any:
    return c.options(path, headers={"Origin": origin, "Access-Control-Request-Method": method,
                                    "Access-Control-Request-Headers": "content-type"})


def test_cors_preflight_from_dashboard_echoes_exact_origin(client: TestClient):
    for method in ("GET", "POST", "PATCH"):
        r = preflight(client, ORIGIN, method)
        assert r.status_code == 200, r.text
        assert r.headers.get_list("access-control-allow-origin") == [ORIGIN]
        assert method in r.headers["access-control-allow-methods"]
        assert "content-type" in r.headers["access-control-allow-headers"].lower()
        assert "access-control-allow-credentials" not in r.headers
        assert "*" not in all_header_values(r)


def test_cors_other_origin_gets_no_allow_origin(client: TestClient):
    for origin in (EVIL, "http://localhost:5174", "https://localhost:5173", "null", ORIGIN + "/"):
        r = preflight(client, origin)
        assert r.status_code == 400
        assert "access-control-allow-origin" not in r.headers
        g = client.get(f"{API}/status", headers={"Origin": origin})
        assert g.status_code == 200 and "access-control-allow-origin" not in g.headers


def test_cors_simple_request_and_disallowed_method(client: TestClient):
    r = client.get(f"{API}/status", headers={"Origin": ORIGIN})
    assert r.headers.get_list("access-control-allow-origin") == [ORIGIN]
    assert "origin" in r.headers.get("vary", "").lower()
    assert "*" not in all_header_values(r)
    assert preflight(client, ORIGIN, "DELETE").status_code == 400  # only GET, POST, PATCH


def test_cors_uses_configured_origin():
    other = "http://127.0.0.1:4000"
    st = make_state(HelixConfig(api=ApiConfig(dashboard_origin=other)))
    with TestClient(secure_app(st, manage_lifecycle=False)) as c:
        assert preflight(c, other).headers.get_list("access-control-allow-origin") == [other]
        assert "access-control-allow-origin" not in preflight(c, ORIGIN).headers


@pytest.mark.parametrize("bad", ["*", "", " http://localhost:5173", "http://a.example, http://b.example",
                                 "http://localhost:5173/app", "ftp://localhost", "localhost:5173",
                                 "http://*.example"])
def test_wildcard_or_malformed_dashboard_origin_refused(bad: str):
    with pytest.raises(ValueError):
        validate_origin(bad)
    try:
        cfg = HelixConfig(api=ApiConfig(dashboard_origin=bad))
    except ValueError:  # the config schema already refuses it
        return
    st = make_state(cfg)
    with pytest.raises(ValueError):
        secure_app(st, manage_lifecycle=False)


# --- WebSocket origin (Req 27.4) ------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"origin": EVIL}, {"origin": "null"}, {"origin": ORIGIN + "/"},
                                     {"origin": "https://localhost:5173"}])
def test_ws_bad_or_missing_origin_closed_1008_before_any_data(client: TestClient, state: AppState, headers):
    for ch in ("events", "cases", "metrics"):
        with pytest.raises(WebSocketDisconnect) as exc:
            # the close is the first ASGI message: the handshake is never accepted, nothing is sent
            with client.websocket_connect(f"/ws/{ch}", headers=headers):
                pass  # pragma: no cover - not reached
        assert exc.value.code == 1008
    assert state.ws_origin_rejected == 3
    assert state.hub.stats()["clients"] in ({}, {"events": 0, "cases": 0, "metrics": 0})
    sec = client.get(f"{API}/status").json()["security"]
    assert sec["ws_origin_rejected"] == 3 and sec["authentication"] is False
    assert client.get(f"{API}/metrics").json()["security"]["ws_origin_rejected"] == 3


def test_ws_dashboard_origin_accepted_and_receives_events():
    st = make_state()
    with TestClient(secure_app(st)) as c:
        with c.websocket_connect("/ws/events", headers={"origin": ORIGIN}) as ws:
            assert c.post(f"{API}/events", json=event_doc()).status_code == 202
            msg = ws.receive_json()
            assert msg["event"]["tx_id"] == "E1"
    assert st.ws_origin_rejected == 0


def test_allow_missing_ws_origin_flag_still_rejects_wrong_origin(state: AppState):
    assert state.allow_missing_ws_origin is False  # default
    with TestClient(secure_app(state, allow_missing_ws_origin=True, manage_lifecycle=False)) as c:
        with c.websocket_connect("/ws/cases"):
            pass  # accepted without an Origin header
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect("/ws/cases", headers={"origin": EVIL}):
                pass  # pragma: no cover
        assert exc.value.code == 1008
    assert state.ws_origin_rejected == 1


# --- security headers -------------------------------------------------------------------------------


def test_security_headers_on_api_responses(client: TestClient):
    for r in (client.get(f"{API}/status"), client.get(f"{API}/cases/C-404"),
              client.post(f"{API}/events", content=b"{bad", headers={"content-type": "application/json"}),
              preflight(client, ORIGIN)):
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"
    r = client.get("/openapi.json")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert "no-store" not in r.headers.get("cache-control", "")


# --- body size limit -----------------------------------------------------------------------------------


def test_oversized_post_is_413_and_nothing_ingested(client: TestClient, state: AppState):
    big = b"x" * (DEFAULT_MAX_BODY_BYTES + 1)
    r = client.post(f"{API}/events", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and "exceeds" in r.json()["detail"]
    assert r.headers["x-content-type-options"] == "nosniff"
    c = state.ingestor.counters
    assert (c.accepted, c.malformed, len(state.ingestor)) == (0, 0, 0)
    # exactly at the limit is not 413 (it is parsed, and rejected as malformed JSON)
    r = client.post(f"{API}/events", content=b"x" * DEFAULT_MAX_BODY_BYTES,
                    headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert state.body_too_large == 1
    assert client.get(f"{API}/status").json()["security"]["body_too_large"] == 1


def test_streamed_body_without_content_length_is_limited():
    st = make_state()
    with TestClient(secure_app(st, max_body_bytes=1024, manage_lifecycle=False)) as c:
        def chunks():
            for _ in range(4):
                yield b"y" * 512

        r = c.post(f"{API}/events", content=chunks(), headers={"content-type": "application/json"})
        assert r.status_code == 413, r.text
        r = c.patch(f"{API}/config", content=b"{" + b" " * 2048 + b"}", headers={"content-type": "application/json"})
        assert r.status_code == 413
        r = c.post(f"{API}/cases/C-1/open", content=chunks(), headers={"content-type": "application/json"})
        assert r.status_code == 413  # body-parameter routes too (not FastAPI's generic 400)
        assert c.post(f"{API}/events", json=event_doc()).status_code == 202  # small bodies pass
    assert st.body_too_large == 3
    assert st.ingestor.counters.accepted == 1


# --- latency breach and "unavailable" (Req 25.6, 25.7) ------------------------------------------------


def test_latency_breach_counter_and_state_in_status_and_metrics():
    now = [1000.0]
    st = make_state(latency=LatencyRecorder(60, now=lambda: now[0]))
    with TestClient(secure_app(st, manage_lifecycle=False)) as c:
        lat = c.get(f"{API}/metrics/latency").json()
        e2e = lat["window"][Stage.END_TO_END.value]
        assert e2e["count"] == 0 and e2e["p95"] == UNAVAILABLE  # Req 25.6
        b = c.get(f"{API}/status").json()["latency_breach"]
        assert b == {"active": False, "count": 0, "threshold_ms": 50.0, "p95_ms": UNAVAILABLE}

        for _ in range(20):
            st.helix.latency.record(Stage.END_TO_END, 80.0)
        for path in ("/status", "/metrics", "/metrics/latency", "/status"):
            b = c.get(f"{API}{path}").json()["latency_breach"]
            assert b["active"] is True and b["count"] == 1 and b["p95_ms"] == 80.0  # counted once per breach

        now[0] += 61.0  # samples leave the 60 s window: breach clears
        b = c.get(f"{API}/status").json()["latency_breach"]
        assert b["active"] is False and b["count"] == 1 and b["p95_ms"] == UNAVAILABLE

        for _ in range(20):
            st.helix.latency.record(Stage.END_TO_END, 120.0)
        assert c.get(f"{API}/metrics").json()["latency_breach"]["count"] == 2


def test_security_stats_shape(client: TestClient):
    sec = client.get(f"{API}/status").json()["security"]
    assert sec == {"authentication": False, "dashboard_origin": ORIGIN, "allow_missing_ws_origin": False,
                   "ws_origin_rejected": 0, "body_too_large": 0}


def test_pipeline_keeps_processing_with_the_secure_app():
    st = make_state()
    with TestClient(secure_app(st)) as c:
        for i in range(5):
            assert c.post(f"{API}/events", json=event_doc(f"E{i}")).status_code == 202
        deadline = time.monotonic() + 10
        while st.helix.seq_no < 5:
            assert time.monotonic() < deadline, "pipeline did not process the events"
            time.sleep(0.01)
