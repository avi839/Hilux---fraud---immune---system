"""Property tests for bounded Cases and the incremental Case aggregates (task 19.1;
Requirements 12, 25)."""

from __future__ import annotations

from datetime import timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from helix.cases import CaseManager, Prioritiser
from helix.cases.manager import stage2_motifs
from helix.config.schema import PrioritiserConfig, RiskConfig
from helix.motifs import MotifType
from helix.risk import best_assessment
from helix.sim.clock import ManualClock
from tests.unit.test_cases import T0, ev, fa, motif

ACCOUNTS = [f"A{i}" for i in range(8)]
MTYPES = [MotifType.MULE_CHAIN, MotifType.FAN_IN, MotifType.FAN_OUT]

account = st.sampled_from(ACCOUNTS)
event_op = st.tuples(st.just("event"), account, account, st.floats(0.6, 1.0), st.integers(1, 999))
motif_op = st.tuples(
    st.just("motif"),
    st.lists(st.integers(0, 40), min_size=1, max_size=3, unique=True),
    st.sampled_from(MTYPES),
    st.floats(0.1, 0.9),
)
ops = st.lists(st.one_of(event_op, motif_op), min_size=1, max_size=40)


def check_invariants(cm: CaseManager) -> None:
    open_cases = cm.open_cases()
    held: dict[str, str] = {}
    for c in open_cases:
        assert len(c.accounts) <= cm.max_accounts, (c.case_id, sorted(c.accounts))
        assert len(c.tx_ids) <= cm.max_events, (c.case_id, len(c.tx_ids))
        for tx in c.tx_ids:
            assert tx not in held, f"{tx} in {held[tx]} and {c.case_id}"  # Req 12.6
            held[tx] = c.case_id
        # Incremental aggregates equal a full rescan (O(1) recompute, Req 12.7, 25).
        assert cm._amount_total(c) == sum(c.amounts.values())
        if c.assessments:
            assert cm._best(c) == best_assessment(list(c.assessments.values()))
        assert cm.stage2_selection(c) == stage2_motifs(c.motifs)
        for a in c.accounts:
            assert c.case_id in cm.account_to_cases[a]
    assert held == cm.tx_to_case


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=ops, max_accounts=st.integers(2, 6), max_events=st.integers(3, 8))
def test_property_cases_stay_bounded_and_aggregates_stay_exact(ops, max_accounts, max_events):
    """**Validates: Requirements 12.1-12.7, 25** For any sequence of alerting events and Motif
    instances whose own size fits the bound, every open Case stays within
    ``case_max_accounts`` / ``case_max_events``, each event is held by at most one open Case,
    and the incrementally kept total, best assessment and stage-2 selection equal a rescan."""
    clock = ManualClock(T0)
    pr = Prioritiser(PrioritiserConfig(alert_budget=5, investigation_cost=0), clock)
    cm = CaseManager(RiskConfig(), pr, clock, max_accounts=max_accounts, max_events=max_events)
    n = 0
    for op in ops:
        clock.advance(timedelta(seconds=1))
        if op[0] == "event":
            _, src, dst, r, cents = op
            n += 1
            tx = f"e{n}"
            cm.on_scored_event(ev(tx, src, dst, amount=f"{cents / 100:.2f}"), fa(tx, r), r)
        else:
            _, raw, mtype, mass = op
            # A Motif edge has fixed endpoints: derive them from the edge index.
            txs = {f"m{i}": (ACCOUNTS[i % 8], ACCOUNTS[(3 * i + 1) % 8], "10.00") for i in raw}
            inst = motif(txs, mtype=mtype, m=mass)
            if len(inst.accounts) > max_accounts or len(inst.tx_ids) > max_events:
                continue  # a single instance larger than the bound is never refused
            cm.on_motif(inst, assessments={t: fa(t, mass) for t in txs})
        check_invariants(cm)
