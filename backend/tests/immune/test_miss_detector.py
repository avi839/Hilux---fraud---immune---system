"""MissDetector (Req 21.1, 21.15) and pipeline integration (Req 21.8) unit tests."""

from __future__ import annotations

from datetime import timedelta

from helix.config import HelixConfig
from helix.core import Helix
from helix.evaluation import InMemoryGroundTruthReader
from helix.immune import ImmuneStage1Engine, ImmuneSystem, attach_immune
from helix.immune.miss_detector import MissDetector
from helix.sim import InMemoryGroundTruth, ManualClock
from helix.sim.ground_truth import GroundTruthRecord, GTSource

from .helpers import T0, make_immune


def _label(sink: InMemoryGroundTruth, tx: str, fraud: bool = True, campaign: str = "K1") -> None:
    sink.write(GroundTruthRecord(event_id=tx, label="FRAUD" if fraud else "LEGIT",
                                 source=GTSource.ATTACKER if fraud else GTSource.GENERATOR, written_at=T0,
                                 campaign_id=campaign if fraud else None))


def test_miss_recorded_once_after_deadline_with_campaign_and_time():
    sink = InMemoryGroundTruth()
    for tx in ("a1", "a2"):
        _label(sink, tx)
    _label(sink, "l1", fraud=False)
    detected = {"a2"}
    calls: list[str] = []
    md = MissDetector(InMemoryGroundTruthReader(sink), detected.__contains__, calls.append)
    for tx in ("a1", "a2", "l1"):
        md.observe(tx, T0)
    assert md.record_misses(T0 - timedelta(seconds=1)) == []  # deadline not reached
    now = T0 + timedelta(minutes=10)
    got = md.record_misses(T0, now=now)
    assert [(m.event_id, m.campaign_id, m.miss_time) for m in got] == [("a1", "K1", now)]
    assert calls == ["a1"]  # only the event id crosses the boundary
    detected.clear()  # a2 was detected at its deadline: settled, never a miss later
    assert md.record_misses(now, now=now) == []
    assert md.recorded == {"a1"} and md.settled == {"a2"}


def test_failing_on_miss_hook_does_not_lose_later_misses():
    sink = InMemoryGroundTruth()
    for tx in ("a1", "a2"):
        _label(sink, tx)

    def boom(tx: str) -> None:
        if tx == "a1":
            raise RuntimeError("x")

    md = MissDetector(InMemoryGroundTruthReader(sink), lambda _tx: False, boom)
    md.observe("a1", T0)
    md.observe("a2", T0)
    assert [m.event_id for m in md.record_misses(T0)] == ["a1", "a2"]
    assert md.hook_errors == 1


def test_attach_immune_shares_state_and_is_idempotent():
    clock = ManualClock(T0)
    helix = Helix(HelixConfig(), clock)
    base = helix.stage1
    imm = make_immune(clock=clock)
    attach_immune(helix, imm)
    eng = helix.stage1
    assert isinstance(eng, ImmuneStage1Engine) and eng.immune is imm
    assert eng.weights is base.weights and eng.profiles is base.profiles
    hooks = len(helix.feedback_hooks)
    attach_immune(helix, imm)
    assert helix.stage1 is eng and len(helix.feedback_hooks) == hooks
    other = ImmuneSystem(imm.cfg, clock)
    attach_immune(helix, other)
    assert helix.stage1.immune is other and helix.immune is other
