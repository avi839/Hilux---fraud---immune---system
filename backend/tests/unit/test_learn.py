"""Unit tests for helix.learn (task 14.1, design §16, Req 19)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from helix.cases import Label
from helix.codec import TransactionEvent
from helix.config import HelixConfig
from helix.config.schema import LearnConfig
from helix.core import Helix
from helix.explain import EntryKind, EvidenceChain, EvidenceEntry
from helix.fusion import Mass
from helix.learn import ROLLBACK_CASE, FeedbackLearner, RollbackRangeError, WeightKind, attach, weight_audit_record
from helix.motifs import MotifEngine, MotifType
from helix.sensors import SensorOutput
from helix.sim.clock import ManualClock
from helix.storage import Storage

T0 = datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc)
FRAUDY = Mass(0.6, 0.1, 0.3)
LEGITY = Mass(0.1, 0.6, 0.3)


def sensor(tx: str, name: str, m: Mass, w: float = 1.0, ctype: str = "SENSOR", minutes: int = 0) -> EvidenceEntry:
    return EvidenceEntry(T0 + timedelta(minutes=minutes), tx, name, EntryKind.CONTRIBUTOR, ctype, m, w)


def motif(tx: str, mid: str, t: MotifType, m: Mass = FRAUDY) -> EvidenceEntry:
    return EvidenceEntry(T0, tx, mid, EntryKind.MOTIF, "MOTIF", m, 1.0, detail={"fuser_id": f"motif:{t.value}"})


def learner(**kw) -> tuple[FeedbackLearner, ManualClock]:
    clock = ManualClock(T0)
    sw = kw.pop("sensor_weights", {"amount": 0.5, "velocity": 0.5, "geo": 0.5})
    return FeedbackLearner(LearnConfig(**kw), clock, sensor_weights=sw,
                           motif_weights=[{t: 0.5 for t in MotifType}]), clock


def test_confirmed_fraud_steps_up_qualifying_contributors_once():
    fl, clock = learner()
    chain = EvidenceChain.of("C-1", [
        sensor("T1", "amount", FRAUDY), sensor("T2", "amount", FRAUDY, minutes=1),  # twice -> once
        sensor("T1", "velocity", LEGITY),  # f <= l: unchanged
        sensor("T1", "canary", Mass(0.95, 0.0, 0.05), ctype="CANARY"),  # never learned
        motif("T2", "M-1", MotifType.FAN_OUT), motif("T2", "M-2", MotifType.FAN_OUT),
        sensor("T1", "ab-1", FRAUDY, w=0.4, ctype="ANTIBODY"),
    ])
    recs = fl.on_label("C-1", Label.CONFIRMED_FRAUD, chain)
    assert {r.contributor_id for r in recs} == {"amount", "motif:FAN_OUT", "ab-1"}
    assert fl.weight("amount") == pytest.approx(0.55)
    assert fl.weight("velocity") == 0.5 and fl.weight("geo") == 0.5
    assert fl.motif_weights[0][MotifType.FAN_OUT] == pytest.approx(0.55)
    assert fl.motif_weights[0][MotifType.FAN_IN] == 0.5
    assert fl.other_weights["ab-1"] == pytest.approx(0.45)  # initial = applied chain weight
    assert "canary" not in fl.sensor_weights and "canary" not in fl.other_weights
    assert all(r.case_id == "C-1" and r.ts == T0 and not r.is_rollback for r in recs)


def test_false_positive_steps_down_and_clamps_to_bounds():
    fl, _ = learner(weight_lower=0.48, learning_rate=0.05)
    chain = EvidenceChain.of("C-1", [sensor("T1", "amount", FRAUDY)])
    fl.on_label("C-1", "FALSE_POSITIVE", chain)
    assert fl.weight("amount") == 0.48
    assert fl.on_label("C-2", "FALSE_POSITIVE", EvidenceChain.of("C-2", [sensor("T9", "amount", FRAUDY)])) == []
    assert fl.weight("amount") == 0.48  # at the bound: no change, no record


def test_repeat_label_is_recorded_as_not_applied():
    fl, _ = learner()
    chain = EvidenceChain.of("C-1", [sensor("T1", "amount", FRAUDY)])
    assert len(fl.on_label("C-1", Label.CONFIRMED_FRAUD, chain)) == 1
    assert fl.on_label("C-1", Label.FALSE_POSITIVE, chain) == []
    assert fl.weight("amount") == pytest.approx(0.55)
    assert [(a.case_id, a.applied) for a in fl.labels] == [("C-1", True), ("C-1", False)]


def test_rollback_restores_latest_at_or_before_t_and_keeps_labels():
    fl, clock = learner()
    c = lambda cid: EvidenceChain.of(cid, [sensor("T1", "amount", FRAUDY)])  # noqa: E731
    clock.advance(timedelta(minutes=10))
    fl.on_label("C-1", Label.CONFIRMED_FRAUD, c("C-1"))  # 0.55 @ +10
    clock.advance(timedelta(minutes=10))
    fl.on_label("C-2", Label.CONFIRMED_FRAUD, c("C-2"))  # 0.60 @ +20
    clock.advance(timedelta(minutes=10))
    recs = fl.rollback(T0 + timedelta(minutes=15))
    assert fl.weight("amount") == pytest.approx(0.55)
    assert recs[0].rollback_to == T0 + timedelta(minutes=15) and recs[0].case_id is None
    fl.rollback(T0 + timedelta(minutes=10))  # exactly at the earliest record
    assert fl.weight("amount") == pytest.approx(0.55)
    assert fl.applied == {"C-1", "C-2"} and len(fl.labels) == 2  # labels kept
    assert [r.new for r in fl.weight_history("amount")] == pytest.approx([0.55, 0.60, 0.55])


def test_rollback_out_of_range_is_rejected_with_range():
    fl, clock = learner()
    with pytest.raises(RollbackRangeError) as ei:
        fl.rollback(T0 + timedelta(seconds=1))  # later than now
    assert ei.value.earliest == T0 and ei.value.latest == T0
    clock.advance(timedelta(minutes=5))
    fl.on_label("C-1", Label.CONFIRMED_FRAUD, EvidenceChain.of("C-1", [sensor("T1", "amount", FRAUDY)]))
    with pytest.raises(RollbackRangeError, match="valid range"):
        fl.rollback(T0)  # earlier than the earliest record
    assert fl.weight("amount") == pytest.approx(0.55)


def test_audit_hook_and_storage_receive_each_record(tmp_path):
    seen = []
    st = Storage(tmp_path / "h.duckdb").open()
    clock = ManualClock(T0)
    fl = FeedbackLearner(LearnConfig(), clock, st, seen.append, sensor_weights={"amount": 0.5, "geo": 0.5})
    fl.on_label("C-1", Label.CONFIRMED_FRAUD, EvidenceChain.of("C-1", [sensor("T1", "amount", FRAUDY)]))
    clock.advance(timedelta(minutes=5))
    fl.on_label("C-2", Label.CONFIRMED_FRAUD, EvidenceChain.of("C-2", [sensor("T2", "geo", FRAUDY)]))
    fl.rollback(T0)  # geo has no record at or before T0: back to its initial value
    assert fl.weight("geo") == 0.5 and fl.weight("amount") == pytest.approx(0.55)
    assert st.flush()
    rows = st.load_rows("weight_history")
    assert [(r["record_no"], r["contributor_id"], r["case_id"], r["rollback"]) for r in rows] == [
        (1, "amount", "C-1", False), (2, "geo", "C-2", False), (3, "geo", None, True)]
    assert rows[0]["old"] == 0.5 and rows[0]["new"] == pytest.approx(0.55) and rows[0]["ts"] == T0
    assert rows[2]["rollback_to"] == T0
    assert seen == [weight_audit_record(r) for r in fl.history]
    assert [(a.case_id, a.action, a.actor) for a in seen] == [
        ("C-1", "weight", "feedback_learner"), ("C-2", "weight", "feedback_learner"),
        (ROLLBACK_CASE, "weight_rollback", "feedback_learner")]
    assert seen[0].prev == {"contributor_id": "amount", "kind": "SENSOR", "weight": 0.5}
    assert seen[0].new["weight"] == pytest.approx(0.55) and seen[0].ts == T0
    assert seen[2].new["rollback_to"] == T0.isoformat()
    st.close()
    # restart: history reloaded, so the rollback range and initial values survive
    st = Storage(tmp_path / "h.duckdb").open()
    fl2 = FeedbackLearner(LearnConfig(), ManualClock(T0), st, sensor_weights={})
    assert [r.kind for r in fl2.history] == [WeightKind.SENSOR] * 3
    assert fl2.initial("geo") == 0.5 and fl2.valid_range()[0] == T0
    fl2.restore_live()
    assert fl2.sensor_weights == pytest.approx({"amount": 0.55, "geo": 0.5})
    st.close()


# --- integration with a real Helix ------------------------------------------------------


class RiskSensor:
    name = "fixed"

    def evaluate(self, ev, dna_before, ctx) -> SensorOutput:
        r = int(ev.mcc) / 100
        m = Mass(2 * r - 1, 0.0, 2 - 2 * r) if r >= 0.5 else Mass(0.0, 1 - 2 * r, 2 * r)
        return SensorOutput(m, {"r": r})


def tx(i: int, r: int, src: str = "A") -> TransactionEvent:
    return TransactionEvent(tx_id=f"T{i:03d}", ts=T0 + timedelta(minutes=i), src=src, dst="B",
                            amount=Decimal("100.00"), currency="EUR", mcc=str(r))


def test_helix_label_updates_weights_for_next_fusion():
    h = Helix(HelixConfig(), ManualClock(T0), sensors=[RiskSensor()], sensor_clock=lambda: 0.0,
              motif_engine=MotifEngine(detectors=()), weights={"fixed": 0.5})
    fl = attach(h)
    assert h.learner is fl and attach(h) is fl and len(h.feedback_hooks) == 1
    first = h.process(tx(1, 95))
    assert first.stage1.contributions[0].weight == 0.5
    h.open_case(first.case_id, "inv")
    h.label(first.case_id, "inv", Label.CONFIRMED_FRAUD)
    assert h.weights["fixed"] == pytest.approx(0.55)
    nxt = h.process(tx(2, 95, src="Z"))
    assert nxt.stage1.contributions[0].weight == pytest.approx(0.55)  # next fusion uses it
    assert [(r.contributor_id, r.case_id) for r in fl.history] == [("fixed", first.case_id)]
    h.open_case(nxt.case_id, "inv")
    h.label(nxt.case_id, "inv", Label.FALSE_POSITIVE)
    assert h.weights["fixed"] == pytest.approx(0.5)
    fl.rollback(T0 + timedelta(minutes=1))
    assert h.weights["fixed"] == pytest.approx(0.55)


def test_attach_clamps_live_weights_into_bounds():
    cfg = HelixConfig(learn=LearnConfig(weight_upper=0.8))
    h = Helix(cfg, ManualClock(T0), sensors=[RiskSensor()], sensor_clock=lambda: 0.0,
              motif_engine=MotifEngine(detectors=()))
    attach(h)
    assert h.weights["fixed"] == 0.8
    assert set(h.motifs.weights.values()) == {0.8} and set(h.cases.motif_weights.values()) == {0.8}


def test_open_case_chain_still_verifies_after_a_motif_weight_is_learned():
    """A learned motif weight reaches an open Case's next stage-2 fusion, and its chain
    (append-only, earlier MOTIF entries kept with the old weight) still verifies."""
    from tests.unit.test_cases import T0 as CT0
    from tests.unit.test_cases import ev, fa, make
    from tests.unit.test_cases import motif as mk_motif

    _, _, cm = make()
    fl = FeedbackLearner(LearnConfig(learning_rate=0.5), ManualClock(CT0), motif_weights=[cm.motif_weights])
    cm.on_scored_event(ev("t1", "A", "B"), fa("t1", 0.65), 0.65)
    cm.on_motif(mk_motif({"t1": ("A", "B", "100"), "t2": ("B", "C", "95")}, m=0.6), {"t2": fa("t2", 0.4)})
    c = cm.get("C-1")
    chain = EvidenceChain.build(c)
    other = EvidenceChain.of("C-9", [motif("T9", "M-9", MotifType.MULE_CHAIN)])
    fl.on_label("C-9", Label.FALSE_POSITIVE, other)
    assert cm.motif_weights[MotifType.MULE_CHAIN] == pytest.approx(0.5)
    cm.on_scored_event(ev("t3", "A", "Z"), fa("t3", 0.9), 0.9)  # new best tx: new stage-2 entry
    assert [x.weight for x in c.case_risk.stage2.contributions if x.contributor_id == "motif:MULE_CHAIN"] == [0.5]
    chain = chain.merge(EvidenceChain.build(c, per_tx_ts={"t3": CT0}))
    s2 = [e for e in chain.of_kind(EntryKind.FUSED) if e.detail.get("stage") == 2]
    assert len(s2) == 2 and sorted(e.detail["motif_weights"] for e in s2) == ["0.5", "1.0"]
    assert [p for p in chain.verify() if "~stage2:" in p] == []
