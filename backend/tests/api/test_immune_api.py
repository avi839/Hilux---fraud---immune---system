"""API tests for the Immune_System wiring: ``GET /immune``, the background miss-detection
task, ``weights`` on ``/metrics`` and the ``/status`` flags. Uses the secure app (same
prefix, middleware and lifecycle as production) through the FastAPI TestClient."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helix.api import AppState, secure_app
from helix.config import HelixConfig
from helix.config.schema import ImmuneConfig
from helix.evaluation import EvaluationMonitor, ground_truth_channel
from helix.sim.clock import ManualClock
from helix.sim.ground_truth import GroundTruthRecord, GTSource

pytestmark = pytest.mark.integration

API = "/api/v1"
START = datetime(2024, 6, 2, tzinfo=timezone.utc)


def const_clock() -> float:
    return 0.0


def immune_state(**kw: Any) -> tuple[AppState, Any]:
    writer, reader = ground_truth_channel(None)
    cfg = HelixConfig(immune=ImmuneConfig(enabled=True, candidates_per_trigger=4, detection_deadline_s=600))
    state = AppState.build(cfg, ManualClock(START), monitor=EvaluationMonitor(reader), gt_writer=writer,
                           gt_reader=reader, sensor_clock=const_clock, **kw)
    state.immune_tick_s = 0.02
    return state, writer


def event(tx: str, ts: datetime, amount: str = "42.50") -> str:
    return json.dumps({"tx_id": tx, "ts": ts.isoformat(), "src": "A-1", "dst": "B-1", "amount": amount,
                       "currency": "EUR", "channel": "card"})


def post(client: TestClient, body: str) -> None:
    r = client.post(f"{API}/events", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 202, r.text


def wait_for(pred, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_immune_disabled_by_default():
    state = AppState.build(HelixConfig(), ManualClock(START), sensor_clock=const_clock)
    assert state.immune is None and state.miss_detector is None
    with TestClient(secure_app(state)) as client:
        r = client.get(f"{API}/immune")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False and body["pool_size"] == 0 and body["pool_max"] == 200
        assert body["recent_antibodies"] == [] and body["recent_misses"] == []
        assert r.headers["X-Content-Type-Options"] == "nosniff"  # same security middleware
        st = client.get(f"{API}/status").json()
        assert st["immune"] == {"enabled": False, "miss_detection": False, "running": False}


def test_metrics_reports_current_sensor_weights():
    state = AppState.build(HelixConfig(), ManualClock(START), sensor_clock=const_clock)
    with TestClient(secure_app(state)) as client:
        w = client.get(f"{API}/metrics").json()["weights"]
    names = {s.name for s in state.helix.stage1.sensors}
    assert set(w) == names and all(v == 1.0 for v in w.values())  # no learning yet: undiscounted

    state.helix.set_weights({"amount": 0.4})
    with TestClient(secure_app(state)) as client:
        w = client.get(f"{API}/metrics").json()["weights"]
    assert w["amount"] == 0.4 and w["velocity"] == 1.0


def test_background_task_records_miss_and_creates_antibodies():
    state, writer = immune_state()
    writer.write(GroundTruthRecord(event_id="ATK1", label="FRAUD", source=GTSource.ATTACKER, written_at=START,
                                   campaign_id="K-1", genome_id="G1"))
    t1 = START + timedelta(minutes=1)
    with TestClient(secure_app(state)) as client:
        assert client.get(f"{API}/status").json()["immune"] == {"enabled": True, "miss_detection": True,
                                                                "running": True}
        post(client, event("ATK1", t1))
        wait_for(lambda: state.helix.seq_no >= 1)
        time.sleep(0.1)  # several ticks: deadline (t1 + 10 min) not reached yet
        assert state.miss_detector.misses == []
        post(client, event("L1", t1 + timedelta(minutes=11)))  # stream time passes the deadline
        wait_for(lambda: len(state.miss_detector.misses) == 1)
        body = client.get(f"{API}/immune", params={"recent": 3}).json()
    assert body["enabled"] is True and body["miss_detection"] is True
    assert body["misses"] == 1
    (m,) = body["recent_misses"]
    assert m["event_id"] == "ATK1" and m["campaign_id"] == "K-1"
    assert body["triggers"] == 1
    assert body["held"] == 4 and body["pool_size"] == 0  # Self_Set below its minimum: held (Req 21.5)
    assert len(body["recent_antibodies"]) == 3
    ab = body["recent_antibodies"][0]
    assert ab["origin"] == "miss:ATK1" and ab["status"] == "HELD" and ab["ab_id"] == "AB000004"
    assert {"ab_id", "status", "origin", "parent_id", "created_at", "tags", "dims", "matches",
            "self_match_rate", "retired_at", "retired_reason"} <= set(ab)
    assert body["pool_max"] == 200 and body["self_set_size"] >= 1


def test_detected_attack_event_is_not_a_miss():
    state, writer = immune_state()
    state.miss_detector._is_detected = lambda tx: True  # every event in an admitted Case
    writer.write(GroundTruthRecord(event_id="ATK1", label="FRAUD", source=GTSource.ATTACKER, written_at=START,
                                   campaign_id="K-1"))
    with TestClient(secure_app(state)) as client:
        post(client, event("ATK1", START))
        post(client, event("L1", START + timedelta(minutes=11)))
        wait_for(lambda: state.miss_detector.settled == {"ATK1"})
        body = client.get(f"{API}/immune").json()
    assert body["misses"] == 0 and body["triggers"] == 0


def test_immune_without_label_reader_has_no_miss_detection():
    state = AppState.build(HelixConfig(), ManualClock(START), immune=True, sensor_clock=const_clock)
    assert state.immune is not None and state.miss_detector is None
    assert state.immune_step() == []
    with TestClient(secure_app(state)) as client:
        body = client.get(f"{API}/immune").json()
    assert body["enabled"] is True and body["miss_detection"] is False


def test_recent_query_is_validated():
    state, _ = immune_state()
    with TestClient(secure_app(state)) as client:
        r = client.get(f"{API}/immune", params={"recent": 0})
    assert r.status_code == 422
