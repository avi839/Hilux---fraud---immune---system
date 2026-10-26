"""Unit tests for helix.core.pipeline (task 8.1)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from helix.cases import CaseStatus, Label
from helix.config import HelixConfig, SeverityTier
from helix.config.schema import SensorConfig
from helix.core import (
    CASES,
    EVENTS,
    LATE_ID,
    METRICS,
    UNAVAILABLE,
    Helix,
    InProcessPublisher,
    LatencyRecorder,
    Stage,
)
from helix.codec import TransactionEvent
from helix.explain import EntryKind, dumps, loads
from helix.fusion import Mass, fuse_stage2
from helix.ingest import AdmittedEvent, Lateness, LatenessKind, StreamIngestor
from helix.motifs import MotifEngine
from helix.replay.engine import LAST_SEQ_KEY, DefaultStage1Engine
from helix.risk import risk
from helix.sensors import SensorOutput
from helix.sim.clock import ManualClock

T0 = datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc)


def const_clock() -> float:
    return 0.0  # sensors never time out: deterministic stage 1


class RiskSensor:
    """Test sensor: the event's ``mcc`` holds the intended risk in percent."""

    name = "fixed"

    def evaluate(self, ev, dna_before, ctx) -> SensorOutput:
        r = int(ev.mcc) / 100 if ev.mcc else 0.5
        m = Mass(2 * r - 1, 0.0, 2 - 2 * r) if r >= 0.5 else Mass(0.0, 1 - 2 * r, 2 * r)
        return SensorOutput(m, {"r": r})


def tx(i: int, src: str = "A", dst: str = "B", amount: str = "100.00", minutes: float | None = None,
       r: int | None = None, **opt) -> TransactionEvent:
    ts = T0 + timedelta(minutes=i if minutes is None else minutes)
    return TransactionEvent(tx_id=f"T{i:03d}", ts=ts, src=src, dst=dst, amount=Decimal(amount),
                            currency="EUR", mcc=None if r is None else str(r), **opt)


def fixed_helix(**kw) -> Helix:
    kw.setdefault("motif_engine", MotifEngine(detectors=()))
    return Helix(HelixConfig(), ManualClock(T0), sensors=[RiskSensor()], sensor_clock=const_clock, **kw)


# --- stage 1 / stage 2 ------------------------------------------------------------------


def test_stage1_matches_replay_engine_and_risk_uses_stage1():
    cfg = HelixConfig(sensors=SensorConfig(min_history_count=2))
    events = [tx(i, dst=f"D{i % 3}", amount=f"{100 + 37 * i}.00", lat=50.0, lon=0.1 * i, channel="web")
              for i in range(12)]
    h = Helix(cfg, ManualClock(T0), sensor_clock=const_clock)
    ref = DefaultStage1Engine(cfg, clock=const_clock)
    for o, ev in zip(h.process_all(events), events):
        assert o.stage1 == ref.process(ev)  # replay recomputes the identical stage-1 result
        assert o.risk == risk(o.stage1)  # Req 13.1
        assert o.stage2.stage == 2
        assert o.stage2 == fuse_stage2(o.stage1, (), cfg.fusion.high_conflict)[0]


def test_stage2_includes_this_events_motif_contributions():
    cfg = HelixConfig(sensors=SensorConfig(min_history_count=2))
    h = Helix(cfg, ManualClock(T0), sensor_clock=const_clock)
    outs = h.process_all([tx(i, src="BOB", dst=f"M{i}", amount="9500.00", minutes=10 * i) for i in range(3)])
    last = outs[-1]
    assert [m.motif_type.value for m in last.motifs] == ["STRUCTURING"]
    assert last.stage2.belief > last.stage1.belief
    assert {c.contributor_id for c in last.stage2.contributions} == {"stage1", "motif:STRUCTURING"}
    case = h.cases.get(last.case_id)
    assert case.tx_ids == {"T000", "T001", "T002"}
    assert case.assessments["T000"] == outs[0].stage1  # stage-1 from the per-tx cache


def test_weights_dict_is_used_for_fusion():
    h = fixed_helix(weights={"fixed": 0.0})
    o = h.process(tx(1, r=95))
    assert o.stage1.uncertainty == 1.0 and o.risk == 0.5
    h.weights["fixed"] = 1.0  # shared with the stage-1 engine
    assert h.process(tx(2, r=95)).risk == pytest.approx(0.95)
    with pytest.raises(ValueError):
        h.set_weights({"fixed": 1.5})


def test_clock_is_event_driven_and_never_moves_back():
    h = fixed_helix()
    h.process(tx(1, minutes=30))
    assert h.clock.now() == T0 + timedelta(minutes=30)
    h.process(tx(2, src="C", minutes=5))  # earlier event of another account
    assert h.clock.now() == T0 + timedelta(minutes=30)


# --- routing, withholding, labels ------------------------------------------------------


def test_alert_creates_case_and_on_high_withholds():
    h = fixed_helix()
    o = h.process(tx(1, r=95))
    assert o.tier is SeverityTier.CRITICAL and o.case_id == "C-1"
    assert h.profiles.withheld("A") == {"T001"}  # on_high: removed from the DNA exactly
    assert h.profiles.view("A").count == 0
    assert h.process(tx(2, r=30)).case_id is None  # below threshold: no Case


def test_withhold_up_front_when_src_has_open_high_case():
    h = fixed_helix()
    h.process(tx(1, r=95))
    o = h.process(tx(2, r=70))
    assert o.withheld_up_front and not o.applied and o.case_id == "C-1"
    assert h.withheld_up_front == 1
    assert h.profiles.withheld("A") == {"T001", "T002"}
    low = h.process(tx(3, r=30))  # below threshold: applied normally
    assert low.applied and not low.withheld_up_front


def test_label_false_positive_releases_and_confirmed_excludes():
    h = fixed_helix()
    seen = []
    h.feedback_hooks.append(lambda case, lab: seen.append((case.case_id, lab)))
    h.process(tx(1, r=95))
    h.process(tx(2, src="X", dst="Y", r=95))
    h.open_case("C-1", "inv")
    h.label("C-1", "inv", "FALSE_POSITIVE", "ok")
    assert h.profiles.withheld("A") == frozenset() and h.profiles.view("A").count == 1
    h.open_case("C-2", "inv")
    h.label("C-2", "inv", Label.CONFIRMED_FRAUD, "")
    assert h.profiles.withheld("X") == frozenset() and h.profiles.view("X").count == 0
    assert seen == [("C-1", Label.FALSE_POSITIVE), ("C-2", Label.CONFIRMED_FRAUD)]
    assert h.cases.get("C-1").status is CaseStatus.CLOSED


# --- evidence chains -------------------------------------------------------------------


def test_evidence_chain_built_with_late_entry_and_verifies():
    h = fixed_helix()
    h.process(tx(1, src="Q", r=30, minutes=60))
    late = AdmittedEvent(tx(2, r=85, minutes=0), Lateness(LatenessKind.LATE, 3600.0), 1, T0)
    o = h.process(late)
    chain = h.chain(o.case_id)
    kinds = {e.kind for e in chain}
    assert {EntryKind.CONTRIBUTOR, EntryKind.FUSED, EntryKind.LATE} <= kinds
    (le,) = chain.of_kind(EntryKind.LATE)
    assert le.contributor_id == LATE_ID and le.tx_id == "T002" and le.detail["lateness_s"] == 3600.0
    assert chain.verify() == []
    assert dumps(loads(dumps(chain))) == dumps(chain)


def test_evidence_chain_is_append_only_across_updates():
    h = fixed_helix()
    h.process(tx(1, r=95))
    before = dict(h.chain("C-1").entries)
    h.process(tx(2, r=70))
    after = h.chain("C-1").entries
    assert all(after[k] == e for k, e in before.items())
    assert {e.tx_id for e in after.values()} == {"T001", "T002"}


# --- storage, publishing, latency --------------------------------------------------------


def test_storage_records_events_assessments_cases(tmp_path):
    from helix.storage import Storage

    st = Storage(tmp_path / "h.duckdb").open()
    h = fixed_helix(storage=st)
    h.process_all([tx(1, r=95), tx(2, r=30)])
    assert st.flush()
    assert [e.event.tx_id for e in st.events()] == ["T001", "T002"]
    assert sorted((a.assessment.tx_id, a.assessment.stage) for a in st.assessments()) == [
        ("T001", 1), ("T001", 2), ("T002", 1), ("T002", 2)]
    (row,) = st.load_rows("cases")
    assert row["case_id"] == "C-1" and row["tier"] == "CRITICAL"
    (ch,) = st.load_rows("evidence_chains")
    assert loads(ch["chain"]).case_id == "C-1"
    assert len(st.load_rows("case_history")) == len(h.cases.get("C-1").history)
    st.close()


def test_runs_without_storage_and_publishes_channels():
    pub = InProcessPublisher(history=100)
    h = fixed_helix(publisher=pub)

    def boom(msg):
        raise RuntimeError("client gone")

    pub.subscribe(EVENTS, boom)  # a failing subscriber never stops processing
    h.process_all([tx(1, r=95), tx(2, r=30)])
    evs = list(pub.history[EVENTS])
    assert [m["seq"] for m in evs] == [1, 2] and pub.errors == 2
    assert evs[0]["case_id"] == "C-1" and evs[0]["event"]["amount"] == "100.00"
    assert evs[1]["case_id"] is None and evs[1]["assessment"]["stage"] == 1
    case_msgs = list(pub.history[CASES])
    assert case_msgs and case_msgs[-1]["case"]["case_id"] == "C-1"
    assert case_msgs[-1]["case"]["amount_at_risk"] == [{"currency": "EUR", "amount": "100.00"}]
    assert len(pub.history[METRICS]) >= 1


def test_latency_recorded_per_stage():
    h = fixed_helix()
    h.process_all([tx(i) for i in range(3)])
    for s in Stage:
        assert h.latency.all_time(s).count == 3
    rep = h.metrics()["latency_ms"]
    assert rep["since_start"]["ACT"]["count"] == 3


def test_latency_recorder_unavailable_and_window_eviction():
    now = [100.0]
    rec = LatencyRecorder(60, now=lambda: now[0])
    d = rec.window(Stage.ACT).to_dict()
    assert d["count"] == 0 and d["p50"] == d["p95"] == d["p99"] == UNAVAILABLE
    rec.record(Stage.ACT, 5.0)
    rec.record(Stage.ACT, 1.0)
    w = rec.window(Stage.ACT)
    assert (w.count, w.p50, w.p99, w.min, w.max) == (2, 1.0, 5.0, 1.0, 5.0)
    now[0] = 161.0
    assert rec.window(Stage.ACT).to_dict()["p95"] == UNAVAILABLE
    assert rec.all_time(Stage.ACT).count == 2
    with pytest.raises(ValueError):
        rec.record(Stage.ACT, float("nan"))


# --- drivers and snapshots ---------------------------------------------------------------


def test_async_run_drains_ingestor():
    clock = ManualClock(T0)
    h = Helix(HelixConfig(), clock, sensors=[RiskSensor()], sensor_clock=const_clock)
    ing = StreamIngestor(HelixConfig().ingest, clock)
    for i in range(4):
        ing.submit(tx(i, r=30))

    async def go():
        return await asyncio.wait_for(h.run(ing, max_events=4), timeout=5)

    assert asyncio.run(go()) == 4
    assert h.seq_no == 4 and h.metrics()["queue_length"] == 0


def test_snapshot_state_delegates_with_seq_no():
    h = fixed_helix()
    h.process_all([tx(i, r=30) for i in range(3)])
    dna, cpd = h.snapshot_state()
    assert dna[LAST_SEQ_KEY] == 3 and "A" in cpd["accounts"]
    assert h.snapshot_state(1)[0][LAST_SEQ_KEY] == 1
