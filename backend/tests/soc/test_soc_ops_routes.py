"""SOC operations routes (helix.api.soc_ops_routes, Req 36, 37) on an app from ``create_app(state)``."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from helix.api import AppState, create_app
from helix.api.soc_ops_routes import include_ops_routes
from helix.sim.clock import ManualClock
from helix.sim.generator import SyntheticGenerator
from helix.sim.ground_truth import InMemoryGroundTruth
from helix.soc import soc_config
from helix.soc.purple import live_fingerprint
from helix.soc.resilience import FaultySensor, ResilientIngestor

pytestmark = pytest.mark.integration

API = "/api/v1"
T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
JSON = {"content-type": "application/json"}


def const_clock() -> float:
    return 0.0


def make_client(events: int = 80) -> tuple[TestClient, AppState]:
    cfg = soc_config()
    cfg = cfg.model_copy(update={"generator": cfg.generator.model_copy(update={"accounts": 200})})
    gt = InMemoryGroundTruth()
    state = AppState.build(cfg, ManualClock(T0), learner=False, sensor_clock=const_clock, gt_writer=gt)
    gen = SyntheticGenerator(cfg.generator, ManualClock(T0), gt, seed=3)
    for _ in range(events):
        state.helix.process(next(gen))
    return TestClient(include_ops_routes(create_app(state), state)), state


def wait_for(pred, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the pipeline")
        time.sleep(0.01)


def tx_doc(i: int, src: str = "R1", minutes: int = 0) -> str:
    return json.dumps({"tx_id": f"RT-{i}", "ts": (T0 + timedelta(days=1, minutes=minutes or i)).isoformat(),
                       "src": src, "dst": "R2", "amount": "42.00", "currency": "EUR"})


def test_console_and_handoff_notes():
    client, state = make_client()
    helix = state.helix
    open_ids = sorted(c.case_id for c in helix.cases.cases.values() if c.is_open)
    assert open_ids
    helix.open_case(open_ids[0], "ana")
    with client:
        doc = client.get(f"{API}/soc/console", params={"time_source": "stream"}).json()
        assert doc["time_source"] == "stream" and doc["window_s"] == 86_400
        assert doc["open_cases"] == len(open_ids) == len(doc["cases"])
        assert doc["sla_targets"]["CRITICAL"] == {"ack_s": 900.0, "close_s": 14_400.0}
        assert doc["workload"] == [{"investigator": "ana", "in_review": 1}]
        assert doc["mttd"]["cases"] >= 1 and doc["mttd"]["mean_s"] >= 0 and doc["mttr"] == {"mean_s": None, "cases": 0}
        row = doc["cases"][0]
        assert {"case_id", "tier", "age_s", "label", "breached_timers", "timers"} <= set(row)
        wall = client.get(f"{API}/soc/console").json()  # wall clock: 2024 Cases are long overdue
        assert wall["time_source"] == "wall" and wall["breached_cases"] == wall["open_cases"]
        assert all(r["label"] == "SLA BREACHED" and r["breached_timers"] for r in wall["cases"])
        assert client.get(f"{API}/soc/console", params={"window_s": 0}).status_code == 422
        assert client.get(f"{API}/soc/console", params={"time_source": "moon"}).status_code == 422

        r = client.post(f"{API}/soc/handoff-notes", json={"author": "ana", "text": "C-1 needs a callback"})
        assert r.status_code == 201 and r.json()["note_id"] == 1 and r.json()["author"] == "ana"
        client.post(f"{API}/soc/handoff-notes", json={"author": "bo", "text": "night shift quiet"})
        bad = client.post(f"{API}/soc/handoff-notes", json={"author": "", "text": "x"})
        assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "author"
        assert client.post(f"{API}/soc/handoff-notes", content=b"[1]", headers=JSON).status_code == 422
        notes = client.get(f"{API}/soc/handoff-notes").json()
        assert notes["total"] == 2 and [n["note_id"] for n in notes["notes"]] == [2, 1]
        assert [n["author"] for n in client.get(f"{API}/soc/console").json()["handoff_notes"]] == ["bo", "ana"]


def test_purple_exercises_scorecards_and_compare():
    client, state = make_client()
    with client:
        before = live_fingerprint(state.helix)
        r = client.post(f"{API}/soc/purple/exercises",
                        json={"name": "fan", "campaigns": [{"ptype": "FAN_OUT", "seed": 1}]})
        assert r.status_code == 201, r.text
        a = r.json()
        assert a["scorecard_id"] == "SC-1" and a["name"] == "fan" and a["metrics"]["campaigns"] == 1
        assert {t["technique_id"] for t in a["techniques"]} >= {"HX-T0002"}
        b = client.post(f"{API}/soc/purple/exercises",
                        json={"campaigns": [{"ptype": "FAN_OUT", "seed": 1}, {"ptype": "STRUCTURING"}],
                              "background_events": 10}).json()
        assert live_fingerprint(state.helix) == before  # live state untouched (Req 37.2)

        bad = client.post(f"{API}/soc/purple/exercises", json={"campaigns": [{"ptype": "NOPE"}]})
        assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "campaigns[0].ptype"

        listed = client.get(f"{API}/soc/purple/scorecards").json()["scorecards"]
        assert [s["scorecard_id"] for s in listed] == ["SC-2", "SC-1"]
        assert client.get(f"{API}/soc/purple/scorecards/SC-1").json()["metrics"] == a["metrics"]
        assert client.get(f"{API}/soc/purple/scorecards/SC-9").status_code == 404
        assert client.get(f"{API}/soc/purple/scorecards/x").status_code == 422

        d = client.get(f"{API}/soc/purple/compare", params={"a": "SC-1", "b": "SC-2"}).json()
        assert d["metrics"]["campaigns"] == {"a": 1, "b": 2, "delta": 1}
        assert d["metrics"]["background_events"]["delta"] == 10
        assert d["techniques"]["HX-T0004"]["campaigns"] == {"a": 0, "b": 1, "delta": 1}
        assert b["metrics"]["events"] - a["metrics"]["events"] == d["metrics"]["events"]["delta"]
        assert client.get(f"{API}/soc/purple/compare", params={"a": "SC-1", "b": "SC-7"}).status_code == 404
        assert client.get(f"{API}/soc/purple/compare", params={"a": "SC-1"}).status_code == 422


def test_resilience_mode_toggle_and_counters():
    client, state = make_client(10)
    helix = state.helix
    with client:
        st = client.get(f"{API}/soc/resilience").json()
        assert st["enabled"] is False and st["counters"]["sensor_failures"] == 0  # disabled at start
        bad = client.post(f"{API}/soc/resilience", json={"enabled": True, "delay_rate": 2})
        assert bad.status_code == 422 and bad.json()["errors"][0]["field"] == "delay_rate"
        assert client.post(f"{API}/soc/resilience", json={"enabled": "yes"}).status_code == 422
        assert client.post(f"{API}/soc/resilience", json={"enabled": True, "colour": 1}).status_code == 422
        assert not isinstance(state.ingestor, ResilientIngestor)  # nothing installed by a rejected request

        on = client.post(f"{API}/soc/resilience", json={"enabled": True, "sensor_failure_rate": 1.0, "seed": 1})
        assert on.status_code == 200 and on.json()["enabled"] is True
        assert isinstance(state.ingestor, ResilientIngestor)
        assert all(isinstance(s, FaultySensor) for s in helix.stage1.sensors)
        n0 = helix.seq_no
        for i in range(1, 4):
            assert client.post(f"{API}/events", content=tx_doc(i), headers=JSON).status_code == 202
        wait_for(lambda: helix.seq_no >= n0 + 3)
        st = client.get(f"{API}/soc/resilience").json()
        assert st["counters"]["sensor_failures"] == 3 * len(helix.stage1.sensors)
        assert st["latency_ms"]["since_start"]["END_TO_END"]["count"] == 3

        # delays: every event is held for 2 later submits; disabling releases the rest
        client.post(f"{API}/soc/resilience", json={"enabled": True, "delay_rate": 1.0, "delay_events": 2})
        for i in range(4, 8):
            r = client.post(f"{API}/events", content=tx_doc(i), headers=JSON)
            assert r.status_code == 202 and r.json()["tx_id"] == f"RT-{i}" and r.json()["status"] == "accepted"
        st = client.get(f"{API}/soc/resilience").json()
        assert st["counters"]["delayed_events"] == 4 and st["queue"]["held"] == 2
        off = client.post(f"{API}/soc/resilience", json={"enabled": False}).json()
        assert off["enabled"] is False and off["released"] == 2 and off["queue"]["held"] == 0
        wait_for(lambda: helix.seq_no >= n0 + 7)
        # seq_no is incremented before REASON adds the edge: wait for the edges themselves
        wait_for(lambda: {f"RT-{i}" for i in range(1, 8)} <= set(helix.graph.edge_ids))
