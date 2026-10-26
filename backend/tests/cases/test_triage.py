"""Auto_Triage (helix.cases.triage, design §26, Req 41.1-41.3): unit tests and Properties 86, 87."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from helix.api.state import AppState
from helix.cases import (
    AutoTriage,
    CaseConflict,
    CaseManager,
    CaseStatus,
    ClosureReason,
    Label,
    Placement,
    Prioritiser,
)
from helix.cases.triage import AUDIT_CATEGORY
from helix.config import HelixConfig, SeverityTier, TriageConfig
from helix.config.schema import PrioritiserConfig, RiskConfig
from helix.learn import FeedbackLearner, attach
from helix.sim.clock import ManualClock

from .helpers import T0, RiskSensor, at, fa, fixed_helix, tx

DAY = 86_400.0


def triage_cfg(after: float = 3_600.0) -> TriageConfig:
    return TriageConfig(enabled=True, auto_clear_after_s=after, sweep_interval_s=60)


# --- unit -------------------------------------------------------------------------------


def test_triage_is_disabled_by_default():
    assert HelixConfig().triage.enabled is False
    state = AppState.build(HelixConfig(), ManualClock(T0), learner=False)
    assert state.triage is None and state.triage_step() == []


def test_idle_low_case_is_auto_cleared_without_a_label_or_feedback():
    h = fixed_helix()
    seen = []
    attach(h)
    h.feedback_hooks.append(lambda case, lab: seen.append(case.case_id))
    o = h.process(tx(1, "A", "B", 65, at(0)))  # LOW
    cid = o.case_id
    case = h.cases.get(cid)
    assert case.tier is SeverityTier.LOW and case.placement is not Placement.NONE
    tri = AutoTriage(triage_cfg(), h.cases)
    assert tri.sweep(at(3_599)) == []  # not idle long enough
    assert tri.sweep(at(3_600)) == [cid]
    assert case.status is CaseStatus.CLOSED and case.closure_reason is ClosureReason.AUTO_CLEARED
    assert case.label is None and case.label_by is None and case.closed_at == at(3_600)
    [entry] = [e for e in case.history if e.action == "auto_clear"]
    assert (entry.prev, entry.new, entry.actor, entry.ts) == ("OPEN", "CLOSED", "system", at(3_600))
    assert case.placement is Placement.NONE
    assert cid not in h.prioritiser.active_ids() and cid not in h.prioritiser.backlog_ids()
    assert seen == [] and h.learner.history == [] and h.learner.labels == []
    # A later event for the same account opens a new Case (Req 12.5 sees no open Case).
    o2 = h.process(tx(2, "A", "B", 65, at(3_700)))
    assert o2.case_id != cid and h.cases.get(o2.case_id).is_open


def test_only_low_open_unescalated_non_canary_cases_are_eligible():
    h = fixed_helix()
    low_open = h.process(tx(1, "A1", "B", 65, at(0))).case_id
    medium = h.process(tx(2, "A2", "B", 75, at(0))).case_id
    claimed = h.process(tx(3, "A3", "B", 65, at(0))).case_id
    escalated = h.process(tx(4, "A4", "B", 65, at(0))).case_id
    canary = h.process(tx(5, "A5", "B", 65, at(0))).case_id
    h.open_case(claimed, "ana")
    h.escalate(escalated, "ana")
    h.cases.canary_cases.add(canary)  # a Canary reference (Req 33.3): never auto-cleared
    tri = AutoTriage(triage_cfg(60), h.cases)
    assert tri.sweep(at(DAY)) == [low_open]
    for cid in (medium, claimed, escalated, canary):
        assert h.cases.get(cid).is_open


def test_auto_clear_needs_an_open_case():
    h = fixed_helix()
    cid = h.process(tx(1, "A", "B", 65, at(0))).case_id
    h.open_case(cid, "ana")
    with pytest.raises(CaseConflict):
        h.cases.auto_clear(cid, at(DAY))
    assert h.cases.get(cid).status is CaseStatus.IN_REVIEW


def test_labelled_and_merged_closures_carry_their_reason():
    h = fixed_helix()
    cid = h.process(tx(1, "A", "B", 65, at(0))).case_id
    h.open_case(cid, "ana")
    h.label(cid, "ana", Label.FALSE_POSITIVE)
    c = h.cases.get(cid)
    assert c.closure_reason is ClosureReason.LABELLED and c.closed_at == at(0)
    s = h.case_summary(c)
    assert s["closure_reason"] == "LABELLED" and s["label"] == "FALSE_POSITIVE"


def test_app_state_triage_step_publishes_audits_and_counts():
    cfg = HelixConfig(triage=triage_cfg(3_600))
    state = AppState.build(cfg, ManualClock(T0), learner=False, soc=True, sensors=[RiskSensor()],
                           sensor_clock=lambda: 0.0)
    h = state.helix
    frames = []
    state.publisher.subscribe("cases", frames.append)
    cid = h.process(tx(1, "A", "B", 65, at(0))).case_id
    assert state.triage is not None
    assert state.triage_step(at(10)) == []  # first step sets the sweep boundary
    assert state.triage_step(at(30)) == []  # before the next sweep_interval_s boundary
    frames.clear()
    assert state.triage_step(at(3_600)) == [cid]
    assert any(f["case"]["case_id"] == cid and f["case"]["closure_reason"] == "AUTO_CLEARED" for f in frames)
    entries = [e for e in state.ledger.entries() if e.category == AUDIT_CATEGORY]
    assert len(entries) == 1 and entries[0].actor == "system"
    assert entries[0].payload["case_id"] == cid and entries[0].payload["idle_s"] == 3_600.0
    assert not [e for e in state.ledger.entries() if e.category == "FEEDBACK_LABEL"]
    w = state.workload_metrics()
    assert w["auto_cleared_total"] == 1 and w["auto_cleared_last_hour"] == 1 and w["labelled_total"] == 0


def test_closure_reason_survives_a_restart(tmp_path):
    from helix.storage import Storage

    p = tmp_path / "triage.duckdb"
    cfg = HelixConfig(triage=triage_cfg(3_600))
    st = Storage(p).open()
    try:
        s1 = AppState.build(cfg, ManualClock(T0), storage=st, restore_state=False, learner=False,
                            sensors=[RiskSensor()], sensor_clock=lambda: 0.0)
        h = s1.helix
        a = h.process(tx(1, "A", "B", 65, at(0))).case_id
        b = h.process(tx(2, "C", "D", 65, at(10))).case_id
        h.open_case(b, "ana")
        h.label(b, "ana", Label.CONFIRMED_FRAUD)
        h.process(tx(3, "E", "F", 95, at(4_000)))  # advances stream time
        assert s1.triage_step(at(4_000)) == [a]
        dna, cpd = h.snapshot_state()
        st.snapshot(dna, cpd, s1.clock.now())
        assert st.flush()
    finally:
        st.close()
    st2 = Storage(p).open()
    try:
        s2 = AppState.build(cfg, ManualClock(T0), storage=st2, learner=False, sensors=[RiskSensor()],
                            sensor_clock=lambda: 0.0)
        ca, cb = s2.helix.cases.get(a), s2.helix.cases.get(b)
        assert ca.closure_reason is ClosureReason.AUTO_CLEARED and ca.label is None and ca.closed_at == at(4_000)
        assert cb.closure_reason is ClosureReason.LABELLED and cb.label is Label.CONFIRMED_FRAUD
        assert a not in s2.helix.cases.tx_to_case.values()
    finally:
        st2.close()


# --- Property 86 --------------------------------------------------------------------------


def view(c):
    return (c.status, c.tier, c.placement, c.assignee, c.label, c.closure_reason, c.escalated, c.withheld,
            c.updated_at, len(c.history), c.risk, c.priority)


@st.composite
def triage_cases(draw):
    """A Case_Manager holding Cases with random tiers, statuses, assignees, escalation,
    Canary references, withheld flags and ``updated_at`` values, plus a sweep time."""
    clock = ManualClock(T0)
    pr = Prioritiser(PrioritiserConfig(alert_budget=draw(st.integers(1, 6)), investigation_cost=0), clock)
    cm = CaseManager(RiskConfig(), pr, clock)
    n = draw(st.integers(1, 12))
    for i in range(n):
        clock.advance(timedelta(seconds=draw(st.integers(0, 3_000))))
        r = draw(st.sampled_from([0.6, 0.65, 0.75, 0.85, 0.95]))
        canary = draw(st.booleans()) and draw(st.booleans())
        ev = tx(i, f"S{i}", f"D{i}", int(r * 100), clock.now())
        cm.on_scored_event(ev, fa(ev.tx_id, r), r, force_case=canary)
        cid = cm.case_of(ev.tx_id)
        action = draw(st.sampled_from(["none", "none", "open", "label", "escalate"]))
        if action in ("open", "label"):
            cm.open(cid, draw(st.sampled_from(["ana", "ben"])))
        if action == "label":
            cm.submit_label(cid, cm.get(cid).assignee, draw(st.sampled_from(list(Label))), "")
        if action == "escalate":
            cm.escalate(cid, "ana")
        case = cm.get(cid)
        if case.is_open and draw(st.booleans()) and draw(st.booleans()):
            case.withheld = True
    after = float(draw(st.integers(60, 6_000)))
    now = clock.now() + timedelta(seconds=draw(st.integers(0, 9_000)))
    return cm, after, now


# Feature: helix-fraud-immune-system, Property 86: Auto-triage closes exactly the eligible Cases
@pytest.mark.property
@settings(suppress_health_check=[HealthCheck.too_slow])
@given(data=triage_cases())
def test_auto_triage_closes_exactly_the_eligible_cases(data):
    """**Validates: Requirements 41.1**"""
    cm, after, now = data
    expected = sorted(
        (c for c in cm.cases.values()
         if c.tier is SeverityTier.LOW and c.status is CaseStatus.OPEN and not c.escalated
         and c.case_id not in cm.canary_cases and not c.withheld
         and (now - c.updated_at).total_seconds() >= after),
        key=lambda c: c.seq_no)
    want = [c.case_id for c in expected]
    before = {cid: view(c) for cid, c in cm.cases.items() if cid not in want}
    hist_n = {cid: len(cm.get(cid).history) for cid in want}
    closed = AutoTriage(triage_cfg(after), cm).sweep(now)
    assert closed == want
    queued = set(cm.prioritiser.active_ids()) | set(cm.prioritiser.backlog_ids())
    for cid in closed:
        c = cm.get(cid)
        assert c.status is CaseStatus.CLOSED and c.closure_reason is ClosureReason.AUTO_CLEARED
        assert c.label is None and c.placement is Placement.NONE and cid not in queued
        new = [(h.action, h.actor) for h in c.history[hist_n[cid]:]]
        assert ("auto_clear", "system") in new and all(actor == "system" for _, actor in new)
    assert {cid: view(cm.get(cid)) for cid in before} == before


# --- Property 87 --------------------------------------------------------------------------


def immune_state(imm) -> str:
    """Full Immune_System state as canonical JSON (its feature vectors hold NaN, and
    ``nan != nan``, so dicts cannot be compared directly)."""
    return json.dumps(imm.to_dict(), sort_keys=True, default=str)


class NoLearningFromAutoClear(RuleBasedStateMachine):
    """Events, open, label, escalate and auto-clear sweeps on a live pipeline with the
    Feedback_Learner and the Immune_System attached."""

    def __init__(self) -> None:
        super().__init__()
        from helix.config.schema import ImmuneConfig
        from helix.immune import ImmuneSystem, attach_immune

        self.clock = ManualClock(T0)
        self.h = fixed_helix(clock=self.clock, weights={"fixed": 0.9})  # room to learn both ways
        self.initial_weights = dict(self.h.weights)
        self.learner = attach(self.h)
        self.immune = ImmuneSystem(ImmuneConfig(enabled=True), self.clock, seed=0)
        attach_immune(self.h, self.immune)
        self.immune_calls: list[str] = []
        orig = self.immune.on_label

        def counted(case, label, **kw):  # noqa: ANN001
            self.immune_calls.append(case.case_id)
            return orig(case, label, **kw)

        self.immune.on_label = counted  # type: ignore[method-assign]
        self.hook_calls: list[tuple[str, Label]] = []
        self.h.feedback_hooks.append(lambda case, lab: self.hook_calls.append((case.case_id, lab)))
        self.stored: list[tuple[str, Label, object, object]] = []  # (case_id, label, chain, ts)
        self.triage = AutoTriage(triage_cfg(60), self.h.cases)
        self.n = 0

    def _open(self) -> list[str]:
        return sorted((c.case_id for c in self.h.cases.open_cases()), key=lambda c: int(c[2:]))

    @rule(acct=st.integers(0, 3), r=st.sampled_from([55, 65, 65, 75, 95]), gap=st.integers(0, 7_200))
    def event(self, acct, r, gap):
        self.n += 1
        self.h.process(tx(self.n, f"A{acct}", f"B{acct}", r, self.clock.now() + timedelta(seconds=gap)))

    @precondition(lambda self: bool(self.h.cases.open_cases()))
    @rule(data=st.data(), label=st.sampled_from(list(Label)))
    def label(self, data, label):
        cid = data.draw(st.sampled_from(self._open()))
        case = self.h.cases.get(cid)
        who = case.assignee or "ana"
        if case.status is CaseStatus.OPEN:
            self.h.open_case(cid, who)
        self.h.label(cid, who, label)
        self.stored.append((cid, label, self.h.chain(cid), self.clock.now()))

    @precondition(lambda self: bool(self.h.cases.open_cases()))
    @rule(data=st.data())
    def open_or_escalate(self, data):
        cid = data.draw(st.sampled_from(self._open()))
        if data.draw(st.booleans()):
            self.h.escalate(cid, "ana")
        elif self.h.cases.get(cid).status is CaseStatus.OPEN:
            self.h.open_case(cid, "ben")

    @rule(gap=st.integers(0, 7_200))
    def sweep(self, gap):
        now = self.clock.now() + timedelta(seconds=gap)
        weights, hist, labels = dict(self.h.weights), list(self.learner.history), list(self.learner.labels)
        pool = immune_state(self.immune)
        calls = (len(self.hook_calls), len(self.immune_calls))
        self.triage.sweep(now)
        self.h._flush_case_changes()
        assert dict(self.h.weights) == weights and self.learner.history == hist and self.learner.labels == labels
        assert immune_state(self.immune) == pool  # Antibody_Pool, Self_Set, lineage: untouched
        assert (len(self.hook_calls), len(self.immune_calls)) == calls

    @invariant()
    def hooks_see_exactly_the_stored_labels(self):
        assert self.hook_calls == [(c, lab) for c, lab, _, _ in self.stored]
        assert self.immune_calls == [c for c, _, _, _ in self.stored]
        cleared = {c.case_id for c in self.h.cases.cases.values() if c.closure_reason is ClosureReason.AUTO_CLEARED}
        assert not cleared & {c for c, _ in self.hook_calls}
        assert all(self.h.cases.get(c).label is None for c in cleared)

    def teardown(self) -> None:
        # Sensor_Weights and weight history equal a run that receives only the stored labels.
        ref = FeedbackLearner(self.h.cfg.learn, ManualClock(T0), sensor_weights=dict(self.initial_weights))
        for cid, lab, chain, ts in self.stored:
            ref.on_label(cid, lab, chain, now=ts)
        assert ref.history == self.learner.history
        assert {k: ref.sensor_weights.get(k, w) for k, w in self.initial_weights.items()} == {
            k: self.h.weights[k] for k in self.initial_weights}


# Feature: helix-fraud-immune-system, Property 87: AUTO_CLEARED never reaches the learner
NoLearningFromAutoClear.TestCase.settings = settings(max_examples=25, stateful_step_count=20, deadline=None,
                                                     suppress_health_check=[HealthCheck.too_slow])
TestAutoClearNeverReachesTheLearner = pytest.mark.property(NoLearningFromAutoClear.TestCase)
TestAutoClearNeverReachesTheLearner.__doc__ = "**Validates: Requirements 41.2, 41.5**"
