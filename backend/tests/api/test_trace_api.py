"""API tests for ``GET /api/v1/trace/{tx_id}`` (Req 38.4, 38.5)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helix.api import AppState, create_app
from helix.config import HelixConfig
from helix.config.schema import SensorConfig
from helix.sim.clock import ManualClock

pytestmark = pytest.mark.integration

API = "/api/v1"
START = datetime(2024, 6, 2, tzinfo=timezone.utc)
SAMPLE = Path(__file__).resolve().parents[3] / "docs" / "sample_transactions.jsonl"
STAGES = ["SENSE", "PERCEIVE", "REMEMBER", "REASON", "ACT", "LEARN"]
TIERS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def const_clock() -> float:
    return 0.0


def wait_for(pred, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the pipeline")
        time.sleep(0.01)


@pytest.fixture
def state() -> AppState:
    cfg = HelixConfig(sensors=SensorConfig(min_history_count=5))
    return AppState.build(cfg, ManualClock(START), sensor_clock=const_clock)


def post_sample(client: TestClient, state: AppState) -> list[dict]:
    """Post the sample file; return the accepted (non-duplicate) documents. The file also
    holds deliberately invalid and duplicate lines."""
    accepted = []
    for ln in SAMPLE.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = client.post(f"{API}/events", content=ln, headers={"content-type": "application/json"})
        if r.status_code == 202 and not r.json()["duplicate"]:
            accepted.append(json.loads(ln))
    assert accepted
    wait_for(lambda: state.helix.seq_no >= len(accepted))
    return accepted


def test_trace_of_a_processed_event_has_every_stage(state: AppState):
    with TestClient(create_app(state)) as client:
        docs = post_sample(client, state)
        for d in docs:
            r = client.get(f"{API}/trace/{d['tx_id']}")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["tx_id"] == d["tx_id"] and body["source"] == "pipeline"
            assert list(body["stages"]) == STAGES  # pipeline order (Req 38.6)
            assert body["event"]["src"] == d["src"] and body["event"]["dst"] == d["dst"]
            st = body["stages"]
            sensors = st["PERCEIVE"]["sensor_results"]
            assert sensors and all({"name", "outcome", "mass"} <= set(s) for s in sensors)
            for s in sensors:  # each Sensor_Agent's Mass_Function sums to 1
                m = s["mass"]
                assert abs(m["fraud"] + m["legit"] + m["uncertainty"] - 1.0) < 1e-9
            for key, stage in (("stage1_assessment", "PERCEIVE"), ("stage2_assessment", "REASON")):
                fa = st[stage][key]
                assert 0.0 <= fa["belief"] <= fa["plausibility"] <= 1.0
            assert isinstance(st["REMEMBER"]["dna_comparison"], dict)
            assert isinstance(st["REASON"]["motifs"], list)
            assert 0.0 <= st["ACT"]["risk_score"] <= 1.0 and st["ACT"]["severity_tier"] in TIERS
        # At least one sample transaction joins a Case, and the trace names it.
        case_ids = {client.get(f"{API}/trace/{d['tx_id']}").json()["stages"]["ACT"]["case_id"] for d in docs}
        case_ids.discard(None)
        assert case_ids
        for cid in case_ids:
            assert client.get(f"{API}/cases/{cid}").status_code == 200


def test_unknown_tx_id_is_404_with_message(state: AppState):
    with TestClient(create_app(state)) as client:
        r = client.get(f"{API}/trace/NOPE-1")
        assert r.status_code == 404
        assert "NOPE-1" in r.json()["detail"] and "unknown Transaction_Event" in r.json()["detail"]
        assert client.get(f"{API}/trace/{'x' * 65}").status_code == 422  # identifier too long


def test_trace_falls_back_to_storage_once_evicted(tmp_path):
    from helix.storage import Storage

    store = Storage(tmp_path / "trace.duckdb").open()
    cfg = HelixConfig(sensors=SensorConfig(min_history_count=5))
    st = AppState.build(cfg, ManualClock(START), storage=store, sensor_clock=const_clock)
    with TestClient(create_app(st)) as client:
        docs = post_sample(client, st)
        assert store.flush()
        tx = docs[0]["tx_id"]
        # Simulate the pipeline having evicted the event from its in-memory window.
        st.helix._recent.pop(tx, None)
        st.helix._pinned.pop(tx, None)
        body = client.get(f"{API}/trace/{tx}").json()
        assert body["source"] == "storage" and list(body["stages"]) == STAGES
        assert body["stages"]["PERCEIVE"]["stage1_assessment"] is not None
        assert body["stages"]["ACT"]["severity_tier"] in TIERS
