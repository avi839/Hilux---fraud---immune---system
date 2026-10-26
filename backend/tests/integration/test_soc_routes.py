"""SOC routes (helix.api.soc_routes): POST /api/v1/auth-events and GET /api/v1/cases/{id}/killchain,
mounted with ``include_soc_routes`` on an app from ``create_app(state)``."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from helix.api import AppState, create_app
from helix.api.soc_routes import include_soc_routes
from helix.config import HelixConfig
from helix.sim.clock import ManualClock
from helix.soc import soc_config
from tests.e2e.test_soc_ato import scenario

pytestmark = pytest.mark.integration

API = "/api/v1"
START = datetime(2024, 6, 1, tzinfo=timezone.utc)
JSON = {"content-type": "application/json"}


def const_clock() -> float:
    return 0.0


def make_client(cfg: HelixConfig) -> tuple[TestClient, AppState]:
    state = AppState.build(cfg, ManualClock(START), learner=False, sensor_clock=const_clock)
    return TestClient(include_soc_routes(create_app(state), state)), state


def wait_for(pred, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the pipeline")
        time.sleep(0.01)


def test_auth_events_and_kill_chain_end_to_end():
    client, state = make_client(soc_config())
    txs, auths = scenario()
    with client:
        for doc in auths:
            r = client.post(f"{API}/auth-events", content=doc, headers=JSON)
            assert r.status_code == 202, r.text
            assert r.headers["X-Backpressure"] == "false"
            assert r.json()["status"] == "accepted" and not r.json()["duplicate"]
        wait_for(lambda: state.helix.auth_processed >= len(auths))
        # (the latest one: stream time moved 8 days, past the 7-day dedup TTL of the first)
        dup = client.post(f"{API}/auth-events", content=auths[-1], headers=JSON)
        assert dup.status_code == 202 and dup.json()["duplicate"] is True
        for doc in txs:
            assert client.post(f"{API}/events", content=doc, headers=JSON).status_code == 202

        def ato_cases() -> list:  # seq_no moves at PERCEIVE start, so wait for ACT's result
            return [c for c in state.helix.cases.open_cases()
                    if "T-ATO" in c.tx_ids and c.kill_chain_stage is not None]

        wait_for(lambda: bool(ato_cases()))
        [case] = ato_cases()
        r = client.get(f"{API}/cases/{case.case_id}/killchain")
        assert r.status_code == 200, r.text
        kc = r.json()
        assert kc["most_advanced"] == "CASH_OUT"
        assert [s["stage"] for s in kc["stages"]] == [
            "RECONNAISSANCE", "CREDENTIAL_ACCESS", "ACCOUNT_TAKEOVER", "PAYEE_MANIPULATION", "CASH_OUT"]
        assert kc["stages"][1]["first_ts"] == "2024-06-09T10:00:00+00:00"
        assert kc["multiplier"]["enabled"] and kc["multiplier"]["applied"] and kc["multiplier"]["value"] == 1.5
        assert kc["multiplier"]["expected_loss"] != kc["multiplier"]["expected_loss_base"]
        assert all(e["mapped"] for e in kc["entries"]) and kc["unmapped"] == []

        # existing routes see the same Case, with the extra kill-chain keys
        summary = client.get(f"{API}/cases/{case.case_id}").json()
        assert summary["case_id"] == case.case_id
        ev = client.get(f"{API}/cases/{case.case_id}/evidence").json()
        assert any(it["name"] == "auth" for it in ev["explanation"]["reasons"]), ev["explanation"]


def test_invalid_auth_event_is_422_and_logged_without_admission():
    client, state = make_client(soc_config())
    with client:
        bad = json.dumps({"event_id": "X", "ts": "not-a-time", "account_id": "A", "event_type": "LOGOUT"})
        r = client.post(f"{API}/auth-events", content=bad, headers=JSON)
        assert r.status_code == 422
        fields = {e["field"] for e in r.json()["errors"]}
        assert fields == {"ts", "session_id", "event_type"}
        assert state.ingestor.auth_counters.accepted == 0 and len(state.ingestor) == 0
        [rec] = state.ingestor.rejected_log.records()
        assert '"LOGOUT"' in rec.raw


def test_auth_route_is_503_with_the_flag_off_and_killchain_404_for_unknown_case():
    client, state = make_client(HelixConfig())
    with client:
        _, auths = scenario()
        r = client.post(f"{API}/auth-events", content=auths[0], headers=JSON)
        assert r.status_code == 503 and "soc.enabled" in r.json()["detail"]
        assert state.ingestor.auth_counters.accepted == 0
        assert client.get(f"{API}/cases/C-404/killchain").status_code == 404
        assert "/api/v1/auth-events" in client.get("/openapi.json").json()["paths"]
