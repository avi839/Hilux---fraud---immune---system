"""Evaluation_Monitor metric properties (Req 14.12, 14.13, 4.20)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.cases.prioritiser import PeriodReport
from helix.evaluation import NOT_APPLICABLE, EvaluationMonitor, InMemoryGroundTruthReader
from helix.sim.ground_truth import GroundTruthRecord, GTSource, InMemoryGroundTruth

pytestmark = pytest.mark.property

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
H = timedelta(hours=1)

amounts = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("999999999.99"), places=2)
events = st.lists(
    st.tuples(st.booleans(), amounts, st.integers(0, 3 * 3600)),  # (is_fraud, amount, offset s)
    min_size=0, max_size=30,
)


@st.composite
def scenario(draw):
    evs = draw(events)
    n = len(evs)
    # Cases: subsets of event indexes admitted in order, at increasing offsets.
    cases = draw(st.lists(st.lists(st.integers(0, max(n - 1, 0)), max_size=6), max_size=8)) if n else []
    return evs, cases


def build(evs):
    sink = InMemoryGroundTruth()
    for i, (fraud, _, _) in enumerate(evs):
        sink.write(GroundTruthRecord(event_id=f"t{i}", label="FRAUD" if fraud else "LEGIT",
                                     source=GTSource.LOADER, written_at=T0))
    m = EvaluationMonitor(InMemoryGroundTruthReader(sink))
    for i, (_, amt, off) in enumerate(evs):
        m.on_event_ingested(f"t{i}", amt, T0 + timedelta(seconds=off))
    return m


def in_unit(x):
    return x == NOT_APPLICABLE or 0.0 <= x <= 1.0


@given(scenario())
def test_captured_fraction_bounded_and_monotone_as_cases_are_admitted(sc):
    """Captured fraud fraction is in [0, 1] (or "not applicable" iff no fraud amount was
    ingested), and never decreases as more Cases are admitted.

    **Validates: Requirements 14.12, 14.13**
    """
    evs, cases = sc
    m = build(evs)
    has_fraud = any(f for f, _, _ in evs)
    end = T0 + 4 * H
    prev = m.captured_fraud_fraction()
    assert (prev == NOT_APPLICABLE) == (not has_fraud)
    for k, idxs in enumerate(cases):
        m.on_case_admitted(f"C-{k}", [f"t{i}" for i in idxs], T0 + 3 * H + timedelta(seconds=k))
        cur = m.captured_fraud_fraction()
        assert in_unit(cur)
        if has_fraud:
            assert cur >= prev
        prev = cur
    r = m.on_period(PeriodReport(period=3, start=T0 + 3 * H, end=end, admitted=len(cases),
                                 backlog_size=0, active_size=0))
    assert r.captured_fraud_fraction == prev
    assert r.fraud_amount_captured <= r.fraud_amount_ingested


@given(scenario())
def test_precision_recall_bounded_and_consistent(sc):
    """Precision and recall are in [0, 1] and match a direct recount over the labels.

    **Validates: Requirements 4.20, 14.12**
    """
    evs, cases = sc
    m = build(evs)
    detected: set[int] = set()
    for k, idxs in enumerate(cases):
        m.on_case_admitted(f"C-{k}", [f"t{i}" for i in idxs], T0 + 3 * H + timedelta(seconds=k))
        detected |= set(idxs)
    mt = m.metrics()
    fraud = {i for i, (f, _, _) in enumerate(evs) if f}
    assert in_unit(mt.precision) and in_unit(mt.recall)
    exp_p = NOT_APPLICABLE if not detected else len(detected & fraud) / len(detected)
    exp_r = NOT_APPLICABLE if not fraud else len(detected & fraud) / len(fraud)
    assert mt.precision == (exp_p if exp_p == NOT_APPLICABLE else pytest.approx(exp_p))
    assert mt.recall == (exp_r if exp_r == NOT_APPLICABLE else pytest.approx(exp_r))
