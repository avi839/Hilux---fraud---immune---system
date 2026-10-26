"""API_Server integration tests (task 11.1): FastAPI TestClient + WebSocket client.

The app lifespan runs the real pipeline worker on the TestClient's event loop, so posted
events are processed asynchronously; tests wait for ``helix.seq_no`` before reading Cases.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from helix.api import AppState, create_app
from helix.config import HelixConfig
from helix.config.schema import GeneratorConfig, SensorConfig
from helix.evaluation import EvaluationMonitor, ground_truth_channel
from helix.sim import SyntheticGenerator
from helix.sim.clock import ManualClock

pytestmark = pytest.mark.integration

SAMPLE = Path(__file__).resolve().parents[3] / "docs" / "sample_transactions.jsonl"
START = datetime(2024, 6, 2, tzinfo=timezone.utc)
API = "/api/v1"
BOB = {"T010", "T011", "T012"}
MULE = {"T013", "T014", "T015"}


def const_clock() -> float:
    return 0.0  # sensors never time out: deterministic stage 1


def make_state(*, storage: Any = None, monitor: Any = None, generator: Any = None, learner: bool = True,
               clock: Optional[ManualClock] = None) -> AppState:
    cfg = HelixConfig(sensors=SensorConfig(min_history_count=5))
    clock = clock if clock is not None else ManualClock(START)
    return AppState.build(cfg, clock, storage=storage, monitor=monitor, generator=generator,
                          learner=learner, sensor_clock=const_clock)


def wait_for(pred, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the pipeline")
        time.sleep(0.01)


def sample_lines() -> list[str]:
    return [ln.rstrip("\r\n") for ln in SAMPLE.read_text(encoding="utf-8").splitlines() if ln.strip()]


def post_sample(client: TestClient, state: AppState) -> list[Any]:
    rs = [client.post(f"{API}/events", content=ln, headers={"content-type": "application/json"})
          for ln in sample_lines()]
    wait_for(lambda: state.helix.seq_no >= 13)
    return rs


def all_cases(client: TestClient, page_size: int = 100) -> list[dict]:
    out, cursor = [], None
    while True:
        params: dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"{API}/cases", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        out += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            return out


def event_doc(tx: str = "E1", **kw: Any) -> dict:
    d = {"tx_id": tx, "ts": "2024-06-03T09:15:00Z", "src": "A-1", "dst": "B-1", "amount": "42.50",
         "currency": "EUR", "channel": "card"}
    d.update(kw)
    return d


@pytest.fixture
def state() -> AppState:
    return make_state()


@pytest.fixture
def client(state: AppState):
    with TestClient(create_app(state)) as c:
        yield c


@pytest.fixture(scope="module")
def sample():
    """Shared read-only run of the sample file through the API."""
    st = make_state()
    with TestClient(create_app(st)) as c:
        responses = post_sample(c, st)
        yield c, st, responses


def find(cases: list[dict], client: TestClient, pred) -> list[dict]:
    out = []
    for c in cases:
        d = client.get(f"{API}/cases/{c['case_id']}").json()
        if pred(set(d["tx_ids"])) and d["status"] != "CLOSED":
            out.append(d)
    return out


# --- ingest + WebSocket -----------------------------------------------------------------------


def test_ingest_then_event_published_on_ws(client: TestClient, state: AppState):
    with client.websocket_connect("/ws/events") as ws:
        r = client.post(f"{API}/events", json=event_doc())
        assert r.status_code == 202
        assert r.json() == {"tx_id": "E1", "status": "accepted", "duplicate": False, "backpressure": False}
        assert r.headers["X-Backpressure"] == "false"
        msg = ws.receive_json()
    assert msg["channel"] == "events" and msg["type"] == "event.scored" and msg["seq"] == 1
    assert msg["event"]["tx_id"] == "E1" and msg["event"]["amount"] == "42.50"
    assert set(msg["assessment"]) >= {"belief", "plausibility", "uncertainty", "conflict", "high_conflict"}
    assert 0.0 <= msg["risk"] <= 1.0
    # the pipeline keeps running after the client disconnected
    assert client.post(f"{API}/events", json=event_doc("E2")).status_code == 202
    wait_for(lambda: state.helix.seq_no == 2)
    assert state.hub.subscribers("events") == 0


def test_duplicate_is_acknowledged_not_readmitted(client: TestClient, state: AppState):
    assert client.post(f"{API}/events", json=event_doc()).json()["duplicate"] is False
    r = client.post(f"{API}/events", json=event_doc())
    assert r.status_code == 202 and r.json()["duplicate"] is True and r.json()["status"] == "duplicate"
    wait_for(lambda: state.helix.seq_no == 1)
    assert state.ingestor.counters.accepted == 1


def test_ws_cases_and_metrics_channels(client: TestClient, state: AppState):
    with client.websocket_connect("/ws/cases") as ws:
        for ln in sample_lines():
            client.post(f"{API}/events", content=ln)
        msg = ws.receive_json()
    assert msg["channel"] == "cases" and msg["type"] == "case.updated"
    assert {"case_id", "status", "tier", "priority", "placement", "amount_at_risk"} <= set(msg["case"])
    assert isinstance(msg["backlog_count"], int)

    wait_for(lambda: state.helix.seq_no >= 13)
    with client.websocket_connect("/ws/metrics") as ws:
        t0 = time.monotonic()
        a, b = ws.receive_json(), ws.receive_json()
        elapsed = time.monotonic() - t0
    assert elapsed < 2.5  # >= 1 Hz while subscribed
    assert a["channel"] == "metrics" and b["seq"] > a["seq"]
    for stage in ("PERCEIVE", "REMEMBER", "REASON", "ACT", "END_TO_END"):
        assert {"count", "mean_ms"} <= set(b["stages"][stage])
    assert b["stages"]["END_TO_END"]["count"] == 13


# --- validation / not found / conflict ------------------------------------------------------


def test_422_lists_each_invalid_field_and_changes_nothing(client: TestClient, state: AppState):
    r = client.post(f"{API}/events", json=event_doc(amount="-20.00", currency="euro"))
    assert r.status_code == 422
    body = r.json()
    assert {e["field"] for e in body["errors"]} == {"amount", "currency"}
    assert all(e["reason"] for e in body["errors"])
    r = client.post(f"{API}/events", content="{not json")
    assert r.status_code == 422 and [e["field"] for e in r.json()["errors"]] == ["$"]
    c = state.ingestor.counters
    assert (c.accepted, c.malformed) == (0, 2)
    assert state.helix.seq_no == 0

    r = client.get(f"{API}/cases", params={"page_size": 0})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "page_size"
    r = client.get(f"{API}/cases", params={"page_size": 101})
    assert r.status_code == 422
    r = client.get(f"{API}/cases", params={"cursor": "garbage!"})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "cursor"


def test_request_models_reject_unknown_fields_and_bad_labels(sample):
    client, st, _ = sample
    cid = next(c.case_id for c in st.helix.cases.cases.values() if c.status.value == "OPEN")
    r = client.post(f"{API}/cases/{cid}/open", json={"investigator": "alice", "role": "admin"})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "role"
    r = client.post(f"{API}/cases/{cid}/feedback", json={"by": "alice", "label": "MAYBE"})
    assert r.status_code == 422 and {e["field"] for e in r.json()["errors"]} == {"label"}
    r = client.post(f"{API}/cases/{cid}/feedback", json={"by": "alice", "label": "FALSE_POSITIVE", "note": "x" * 2001})
    assert r.status_code == 422 and r.json()["errors"][0]["field"] == "note"
    assert st.helix.cases.cases[cid].status.value == "OPEN"


def test_404_for_unknown_case(client: TestClient, state: AppState):
    for method, path, body in [
        ("GET", "/cases/C-999", None), ("GET", "/cases/C-999/evidence", None),
        ("GET", "/cases/C-999/subgraph", None), ("GET", "/cases/C-999/timeline", None),
        ("POST", "/cases/C-999/open", {"investigator": "alice"}),
        ("POST", "/cases/C-999/escalate", {"by": "alice"}),
        ("POST", "/cases/C-999/feedback", {"by": "alice", "label": "CONFIRMED_FRAUD"}),
    ]:
        r = client.request(method, f"{API}{path}", json=body)
        assert r.status_code == 404, (path, r.text)
        assert r.json()["case_id"] == "C-999"
    assert state.helix.cases.cases == {}


def test_409_wrong_assignee_and_closed_case(client: TestClient, state: AppState):
    post_sample(client, state)
    bob = find(all_cases(client), client, lambda txs: txs == BOB)[0]
    cid = bob["case_id"]
    r = client.post(f"{API}/cases/{cid}/open", json={"investigator": "alice"})
    assert r.status_code == 200 and r.json()["status"] == "IN_REVIEW" and r.json()["assignee"] == "alice"
    before = client.get(f"{API}/cases/{cid}").json()

    r = client.post(f"{API}/cases/{cid}/open", json={"investigator": "bob"})
    assert r.status_code == 409
    assert r.json()["assignee"] == "alice" and "alice" in r.json()["detail"]
    r = client.post(f"{API}/cases/{cid}/feedback", json={"by": "bob", "label": "CONFIRMED_FRAUD"})
    assert r.status_code == 409
    assert client.get(f"{API}/cases/{cid}").json() == before  # unchanged

    r = client.post(f"{API}/cases/{cid}/feedback", json={"by": "alice", "label": "CONFIRMED_FRAUD", "note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "CLOSED" and r.json()["label"] == "CONFIRMED_FRAUD"
    for path, body in [("open", {"investigator": "alice"}), ("escalate", {"by": "alice"})]:
        assert client.post(f"{API}/cases/{cid}/{path}", json=body).status_code == 409


def test_escalate_sets_critical(client: TestClient, state: AppState):
    post_sample(client, state)
    mule = find(all_cases(client), client, lambda txs: MULE <= txs)[0]
    r = client.post(f"{API}/cases/{mule['case_id']}/escalate", json={"by": "carol"})
    assert r.status_code == 200 and r.json()["tier"] == "CRITICAL" and r.json()["escalated"] is True
    assert r.json()["history"][-1]["action"] == "escalate"


# --- pagination -------------------------------------------------------------------------------


def test_pagination_tokens(sample):
    client, st, _ = sample
    ids = sorted(st.helix.cases.cases, key=lambda c: int(c.split("-")[1]))
    assert len(ids) >= 3
    r = client.get(f"{API}/cases")
    assert r.json()["page_size"] == 50 and r.json()["next_cursor"] is None
    seen = [c["case_id"] for c in all_cases(client, page_size=1)]
    assert seen == ids
    first = client.get(f"{API}/cases", params={"page_size": 1}).json()
    assert first["total"] == len(ids) and len(first["items"]) == 1 and first["next_cursor"]
    alias = client.get(f"{API}/cases", params={"page_size": 1, "page_token": first["next_cursor"]}).json()
    assert alias["items"][0]["case_id"] == ids[1]


# --- config -----------------------------------------------------------------------------------


def test_patch_config_range_error_is_all_or_nothing(client: TestClient, state: AppState):
    before = client.get(f"{API}/config").json()
    assert set(before["runtime"]) == {"prioritiser.alert_budget", "prioritiser.investigation_cost"}

    r = client.patch(f"{API}/config", json={"prioritiser.alert_budget": 0, "prioritiser.investigation_cost": 10})
    assert r.status_code == 422
    (err,) = r.json()["errors"]
    assert err["field"] == "prioritiser.alert_budget"
    assert err["permitted"]["lo"] == 1 and err["permitted"]["hi"] == 1000
    assert "[1, 1000]" in err["reason"]
    for doc in ({"fusion.high_conflict": 0.5}, {"nope.x": 1}, {"prioritiser.alert_budget": 2.5},
                {"prioritiser.alert_budget": True}, {}):
        assert client.patch(f"{API}/config", json=doc).status_code == 422, doc
    after = client.get(f"{API}/config").json()
    assert after == before and after["pending"] == {}

    r = client.patch(f"{API}/config", json={"prioritiser": {"alert_budget": 30}})
    assert r.status_code == 200
    assert r.json()["accepted"] == {"prioritiser.alert_budget": 30} and r.json()["applies_from_period"] == 1
    cfg = client.get(f"{API}/config").json()
    assert cfg["config"]["prioritiser"]["alert_budget"] == 30
    assert cfg["pending"] == {"alert_budget": 30} and cfg["effective"]["prioritiser.alert_budget"] == 20


# --- metrics / latency / status ---------------------------------------------------------------


def test_latency_unavailable_before_any_event(client: TestClient, state: AppState):
    body = client.get(f"{API}/latency").json()
    for scope in ("window", "since_start"):
        for stage in ("PERCEIVE", "REMEMBER", "REASON", "ACT", "END_TO_END"):
            s = body[scope][stage]
            assert s["count"] == 0
            assert s["p50"] == s["p95"] == s["p99"] == "unavailable"
    assert body["window_s"] == 60.0 and body["queue_length"] == 0
    assert body["latency_breach"]["active"] is False
    assert client.get(f"{API}/metrics/latency").status_code == 200

    client.post(f"{API}/events", json=event_doc())
    wait_for(lambda: state.helix.seq_no == 1)
    e2e = client.get(f"{API}/latency").json()["window"]["END_TO_END"]
    assert e2e["count"] == 1 and isinstance(e2e["p95"], float)


def test_metrics_and_status(sample):
    client, st, _ = sample
    m = client.get(f"{API}/metrics").json()
    assert m["loop"]["processed"] == 13
    assert m["ingest"]["accepted"] == 13 and m["ingest"]["duplicate"] == 1 and m["ingest"]["malformed"] == 1
    s = client.get(f"{API}/status").json()
    assert s["status"] == "ok" and s["worker"]["running"] is True and s["storage"] is None
    assert s["learner"] is True and s["generator"] == {"configured": False, "running": False}


def test_monitor_is_wired_to_the_pipeline():
    _, reader = ground_truth_channel(None)
    mon = EvaluationMonitor(reader)
    st = make_state(monitor=mon)
    with TestClient(create_app(st)) as c:
        post_sample(c, st)
        # ``seq_no`` is bumped when an event *starts* processing; the monitor hears about it
        # only when the pipeline publishes the event and then its Case changes at the end of
        # ``process``. Wait for everything asserted below, not just the event count.
        wait_for(lambda: mon.metrics().ingested_events >= 13 and mon.metrics().cases_admitted >= 1
                 and mon.detected_events() >= BOB)
        mt = mon.metrics()
        assert mt.ingested_events == 13
        assert mt.cases_admitted >= 1
        assert mon.detected_events() >= BOB
        assert c.get(f"{API}/metrics").json()["evaluation"]["ingested_events"] == 13


# --- OpenAPI ----------------------------------------------------------------------------------


def test_openapi_lists_every_endpoint(client: TestClient):
    doc = client.get("/openapi.json").json()
    paths = doc["paths"]
    expected = {
        ("post", "/events"), ("get", "/cases"), ("get", "/cases/{case_id}"),
        ("get", "/cases/{case_id}/evidence"), ("get", "/cases/{case_id}/subgraph"),
        ("post", "/cases/{case_id}/expand"), ("get", "/cases/{case_id}/timeline"),
        ("get", "/cases/{case_id}/timeline/at"), ("post", "/cases/{case_id}/open"),
        ("post", "/cases/{case_id}/feedback"), ("post", "/cases/{case_id}/escalate"),
        ("get", "/config"), ("patch", "/config"), ("get", "/metrics"), ("get", "/latency"),
        ("post", "/weights/rollback"), ("post", "/sim/generator/start"), ("post", "/sim/generator/stop"),
        ("get", "/status"),
    }
    for method, p in expected:
        assert method in paths.get(API + p, {}), (method, p)
    ops = {(m, p): op for p, item in paths.items() for m, op in item.items()}
    assert "requestBody" in ops[("post", f"{API}/events")]
    assert {"404", "409", "422"} <= set(ops[("post", f"{API}/cases/{{case_id}}/open")]["responses"])
    assert "422" in ops[("patch", f"{API}/config")]["responses"]
    params = {q["name"] for q in ops[("get", f"{API}/cases")]["parameters"]}
    assert {"page_size", "cursor"} <= params


# --- the sample file end to end ---------------------------------------------------------------


def test_sample_file_through_the_api_produces_the_three_cases(sample):
    client, st, responses = sample
    codes = [r.status_code for r in responses]
    assert codes.count(202) == 14 and codes.count(422) == 1
    assert sum(r.json().get("duplicate", False) for r in responses if r.status_code == 202) == 1
    bad = next(r for r in responses if r.status_code == 422)
    assert {e["field"] for e in bad.json()["errors"]} == {"amount", "currency"}

    cases = all_cases(client)
    alice = find(cases, client, lambda txs: "T007" in txs)
    bob = find(cases, client, lambda txs: txs == BOB)
    mule = find(cases, client, lambda txs: MULE <= txs)
    assert len(alice) == len(bob) == len(mule) == 1
    assert alice[0]["tier"] in ("HIGH", "CRITICAL")
    assert "STRUCTURING" in bob[0]["motifs"] and "MULE_CHAIN" in mule[0]["motifs"]
    assert len({alice[0]["case_id"], bob[0]["case_id"], mule[0]["case_id"]}) == 3

    ev = client.get(f"{API}/cases/{alice[0]['case_id']}/evidence").json()
    assert ev["chain"]["case_id"] == alice[0]["case_id"] and ev["chain"]["entries"]
    ex = ev["explanation"]
    assert set(ex) >= {"summary", "reasons", "mitigating", "insufficient", "notes"}
    assert ex["summary"].startswith(alice[0]["tier"])
    names = " ".join(i["name"] for i in ex["reasons"])
    assert all(s in names for s in ("amount", "geo", "counterparty"))


def test_subgraph_and_expand(sample):
    client, st, _ = sample
    mule = find(all_cases(client), client, lambda txs: MULE <= txs)[0]
    cid = mule["case_id"]
    sub = client.get(f"{API}/cases/{cid}/subgraph", params={"hops": 1}).json()
    accounts = {n["account"] for n in sub["nodes"]}
    assert set(mule["accounts"]) <= accounts and sub["hops"] == 1
    assert {e["tx_id"] for e in sub["edges"]} >= MULE
    assert all("amount" in e and "ts" in e for e in sub["edges"])
    assert client.get(f"{API}/cases/{cid}/subgraph", params={"hops": 5}).status_code == 422

    r = client.post(f"{API}/cases/{cid}/expand", json={"account": "ACC-DAVE"})
    assert r.status_code == 200
    assert r.json()["nodes"][0]["account"] == "ACC-DAVE"
    assert {n["account"] for n in r.json()["nodes"][1:]} == {"ACC-CARL", "ACC-ERIN"}
    r = client.post(f"{API}/cases/{cid}/expand", json={"account": "ACC-NOBODY"})
    assert r.status_code == 404 and r.json()["account"] == "ACC-NOBODY"
    # replay without Storage
    assert client.get(f"{API}/cases/{cid}/timeline").status_code == 503


def test_timeline_and_replay_out_of_range_with_storage(tmp_path):
    from helix.storage import Storage

    store = Storage(tmp_path / "api.duckdb").open()
    st = make_state(storage=store)
    with TestClient(create_app(st)) as c:
        post_sample(c, st)
        assert store.flush()
        mule = find(all_cases(c), c, lambda txs: MULE <= txs)[0]
        cid = mule["case_id"]
        tl = c.get(f"{API}/cases/{cid}/timeline").json()
        assert {e["event"]["tx_id"] for e in tl["entries"]} >= MULE
        start, end = datetime.fromisoformat(tl["start"]), datetime.fromisoformat(tl["end"])

        late = (end + timedelta(days=1)).isoformat()
        r = c.get(f"{API}/cases/{cid}/timeline/at", params={"t": late})
        assert r.status_code == 422
        body = r.json()
        assert body["errors"][0]["field"] == "t"
        assert datetime.fromisoformat(body["start"]) == start and datetime.fromisoformat(body["end"]) == end

        r = c.get(f"{API}/cases/{cid}/timeline/at", params={"t": end.isoformat()})
        assert r.status_code == 200, r.text
        assert {e["event"]["tx_id"] for e in r.json()["entries"]} >= MULE
        assert c.get(f"{API}/cases/{cid}/timeline/at", params={"t": "2024-06-06T14:00:00"}).status_code == 422
        assert c.get(f"{API}/status").json()["storage"]["configured"] is True
    assert not store.is_open  # lifespan shutdown flushed and closed Storage


# --- weights / generator ------------------------------------------------------------------------


def test_weight_rollback(client: TestClient, state: AppState):
    r = client.post(f"{API}/weights/rollback", json={"t": "2000-01-01T00:00:00Z"})
    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "t" and "start" in r.json() and "end" in r.json()
    r = client.post(f"{API}/weights/rollback", json={"t": START.isoformat()})
    assert r.status_code == 200 and r.json()["records"] == []
    assert client.post(f"{API}/weights/rollback", json={"t": "2024-06-02T00:00:00"}).status_code == 422


def test_weight_rollback_without_learner_is_501():
    st = make_state(learner=False)
    with TestClient(create_app(st)) as c:
        r = c.post(f"{API}/weights/rollback", json={"t": START.isoformat()})
        assert r.status_code == 501


def test_generator_start_stop():
    clock = ManualClock(START)
    writer, _ = ground_truth_channel(None)
    gen = SyntheticGenerator(GeneratorConfig(accounts=20, rate_eps=100), clock, writer, seed=7)
    st = make_state(generator=gen, clock=clock)
    with TestClient(create_app(st)) as c:
        r = c.post(f"{API}/sim/generator/start")
        assert r.json() == {"running": True, "changed": True}
        assert c.post(f"{API}/sim/generator/start").json()["changed"] is False
        wait_for(lambda: st.helix.seq_no >= 10, timeout=5)
        assert c.get(f"{API}/status").json()["generator"]["running"] is True
        assert c.post(f"{API}/sim/generator/stop").json() == {"running": False, "changed": True}
        assert c.post(f"{API}/sim/generator/stop").json()["changed"] is False
        n = st.ingestor.counters.accepted
        time.sleep(0.2)
        assert st.ingestor.counters.accepted == n  # feeder stopped
    assert st.ingestor.counters.malformed == 0


def test_generator_endpoints_without_generator_are_409(client: TestClient):
    assert client.post(f"{API}/sim/generator/start").status_code == 409
    assert client.post(f"{API}/sim/generator/stop").status_code == 409


def test_ws_frames_are_json_with_decimal_strings(client: TestClient):
    with client.websocket_connect("/ws/events") as ws:
        client.post(f"{API}/events", json=event_doc(amount=1234.5))
        raw = ws.receive_text()
    msg = json.loads(raw)
    assert msg["event"]["amount"] == "1234.50"
