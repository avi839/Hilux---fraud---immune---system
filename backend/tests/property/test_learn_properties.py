"""Property tests for helix.learn (design Properties 57-60)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.cases import Label
from helix.config.schema import LearnConfig
from helix.explain import EntryKind, EvidenceChain, EvidenceEntry
from helix.fusion import Mass
from helix.learn import FeedbackLearner, RollbackRangeError
from helix.motifs import MotifType
from helix.sim.clock import ManualClock

pytestmark = pytest.mark.property

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
SENSORS = ["amount", "velocity", "geo", "device"]
MOTIFS = [MotifType.FAN_OUT, MotifType.ROUND_TRIP]
ANTIBODIES = ["ab-1", "ab-2"]
EPS = 1e-12


@st.composite
def masses(draw) -> Mass:
    f = draw(st.sampled_from([0.0, 0.1, 0.3, 0.5, 0.7]))
    l = draw(st.sampled_from([0.0, 0.1, 0.3]))  # noqa: E741
    return Mass(f, l, round(1.0 - f - l, 10))


@st.composite
def chains(draw, case_id: str) -> EvidenceChain:
    entries = []
    for i in range(draw(st.integers(1, 3))):
        tx = f"{case_id}-T{i}"
        for name in draw(st.lists(st.sampled_from(SENSORS + ["canary"]), unique=True, max_size=5)):
            entries.append(EvidenceEntry(T0, tx, name, EntryKind.CONTRIBUTOR,
                                         "CANARY" if name == "canary" else "SENSOR", draw(masses()), 1.0))
        for ab in draw(st.lists(st.sampled_from(ANTIBODIES), unique=True, max_size=2)):
            entries.append(EvidenceEntry(T0, tx, ab, EntryKind.CONTRIBUTOR, "ANTIBODY", draw(masses()), 0.5))
        for j, t in enumerate(draw(st.lists(st.sampled_from(MOTIFS), max_size=2))):
            entries.append(EvidenceEntry(T0, tx, f"{case_id}-M{i}{j}", EntryKind.MOTIF, "MOTIF", draw(masses()),
                                         1.0, detail={"fuser_id": f"motif:{t.value}"}))
    return EvidenceChain.of(case_id, entries)


configs = st.builds(
    lambda lo, span, lr: LearnConfig(weight_lower=lo, weight_upper=min(1.0, lo + span), learning_rate=lr),
    st.sampled_from([0.0, 0.1, 0.3]), st.sampled_from([0.1, 0.4, 0.9]), st.sampled_from([0.01, 0.05, 0.2, 0.5]),
)

# op = ("label", case_id, label, chain) | ("rollback", minutes_back) ; each op advances the clock
ops = st.lists(
    st.one_of(
        st.tuples(st.just("label"), st.sampled_from(["C-1", "C-2", "C-3", "C-4"]),
                  st.sampled_from(list(Label))),
        st.tuples(st.just("rollback"), st.integers(0, 120)),
    ),
    min_size=1, max_size=12,
)


def make(cfg: LearnConfig, start: float) -> tuple[FeedbackLearner, ManualClock]:
    clock = ManualClock(T0)
    w = min(cfg.weight_upper, max(cfg.weight_lower, start))
    fl = FeedbackLearner(cfg, clock, sensor_weights={s: w for s in SENSORS},
                         motif_weights=[{t: w for t in MotifType}], other_weights={a: w for a in ANTIBODIES})
    return fl, clock


KEYS = SENSORS + [f"motif:{t.value}" for t in MOTIFS] + ANTIBODIES


def snapshot(fl: FeedbackLearner) -> dict[str, float]:
    return {k: fl.weight(k) for k in KEYS}


def run(fl: FeedbackLearner, clock: ManualClock, data, seq, check=None) -> None:
    for op in seq:
        clock.advance(timedelta(minutes=7))
        before = snapshot(fl)
        if op[0] == "label":
            chain = data.draw(chains(op[1]))
            fl.on_label(op[1], op[2], chain)
            if check:
                check("label", before, snapshot(fl))
        else:
            t = clock.now() - timedelta(minutes=op[1])
            try:
                fl.rollback(t)
            except RollbackRangeError:
                assert snapshot(fl) == before
            if check:
                check("rollback", before, snapshot(fl))


@settings(max_examples=150, deadline=None)
@given(cfg=configs, start=st.floats(0.0, 1.0), seq=ops, data=st.data())
def test_p57_weight_bounds_and_step_size(cfg, start, seq, data):
    """Feature: helix-fraud-immune-system, Property 57: Weight bounds and step size.

    **Validates: Requirements 19.4, 19.6**
    """
    fl, clock = make(cfg, start)

    def check(kind, before, after):
        for k, w in after.items():
            assert cfg.weight_lower - EPS <= w <= cfg.weight_upper + EPS, (k, w)
            if kind == "label":
                assert abs(w - before[k]) <= cfg.learning_rate + EPS, (k, before[k], w)

    run(fl, clock, data, seq, check)
    assert "canary" not in fl.sensor_weights


@settings(max_examples=150, deadline=None)
@given(cfg=configs, start=st.floats(0.0, 1.0), label=st.sampled_from(list(Label)), data=st.data())
def test_p58_only_qualifying_contributors_learn(cfg, start, label, data):
    """Feature: helix-fraud-immune-system, Property 58: Only qualifying contributors learn.

    **Validates: Requirements 19.1, 19.2, 19.5, 19.12**
    """
    fl, _ = make(cfg, start)
    chain = data.draw(chains("C-1"))
    qualifying: set[str] = set()
    for e in chain.contributors():
        if e.ctype == "CANARY" or e.mass.f <= e.mass.l:
            continue
        qualifying.add(str(e.detail["fuser_id"]) if e.kind is EntryKind.MOTIF else e.contributor_id)
    before = snapshot(fl)
    recs = fl.on_label("C-1", label, chain)
    after = snapshot(fl)
    step = cfg.learning_rate if label is Label.CONFIRMED_FRAUD else -cfg.learning_rate
    for k in KEYS:
        expected = min(cfg.weight_upper, max(cfg.weight_lower, before[k] + step)) if k in qualifying else before[k]
        assert after[k] == pytest.approx(expected, abs=EPS), k
    assert {r.contributor_id for r in recs} <= qualifying
    assert len(recs) == len({r.contributor_id for r in recs})  # one change per contributor
    assert "canary" not in fl.sensor_weights and "canary" not in fl.other_weights


@settings(max_examples=150, deadline=None)
@given(cfg=configs, start=st.floats(0.0, 1.0), seq=ops, back=st.integers(-30, 200), data=st.data())
def test_p59_rollback_restores_latest_at_or_before_t(cfg, start, seq, back, data):
    """Feature: helix-fraud-immune-system, Property 59: Rollback restores the latest value at or before T.

    **Validates: Requirements 19.9, 19.10**
    """
    fl, clock = make(cfg, start)
    initial = snapshot(fl)
    run(fl, clock, data, seq)
    clock.advance(timedelta(minutes=1))
    now = clock.now()
    t = now - timedelta(minutes=back)
    history = list(fl.history)
    labels, applied = list(fl.labels), set(fl.applied)
    earliest = min((r.ts for r in history), default=T0)
    before = snapshot(fl)
    if not (earliest <= t <= now):
        with pytest.raises(RollbackRangeError) as ei:
            fl.rollback(t)
        assert (ei.value.earliest, ei.value.latest) == (earliest, now)
        assert snapshot(fl) == before and fl.history == history
        return
    fl.rollback(t)
    for k in KEYS:
        recs = [r.new for r in history if r.contributor_id == k and r.ts <= t]
        assert fl.weight(k) == pytest.approx(recs[-1] if recs else initial[k], abs=EPS), k
    assert fl.labels == labels and fl.applied == applied  # labels kept
    assert all(r.is_rollback and r.rollback_to == t for r in fl.history[len(history):])


@settings(max_examples=150, deadline=None)
@given(cfg=configs, start=st.floats(0.0, 1.0), seq=ops, data=st.data())
def test_p60_at_most_one_label_applied_per_case(cfg, start, seq, data):
    """Feature: helix-fraud-immune-system, Property 60: At most one label applied per Case.

    **Validates: Requirements 19.11**
    """
    fl, clock = make(cfg, start)

    def check(kind, before, after):
        last = fl.labels[-1] if kind == "label" else None
        if last is not None and not last.applied:
            assert after == before and last.records == ()

    run(fl, clock, data, seq, check)
    applied = [a.case_id for a in fl.labels if a.applied]
    assert len(applied) == len(set(applied))
    by_case = {r.case_id for r in fl.history if r.case_id is not None}
    assert by_case <= set(applied)
    for a in fl.labels:
        if not a.applied:
            assert a.case_id in applied  # a repeat of an earlier applied label
