"""Property tests for helix.cases (design Properties 28, 42-48, 56)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.cases import (
    MAX_NOTE_CHARS,
    CaseConflict,
    CaseError,
    CaseKey,
    CaseManager,
    CaseStatus,
    Label,
    Placement,
    Prioritiser,
    PrioritiserChangeError,
)
from helix.codec import TransactionEvent
from helix.config import SeverityTier
from helix.config.schema import PrioritiserConfig, RiskConfig
from helix.fusion import FusedAssessment, Mass
from helix.graph import Edge
from helix.motifs import MotifInstance, MotifType, motif_mass
from helix.sim.clock import ManualClock

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
ACCOUNTS = "ABCDE"
N_TX = 14


def fa(tx: str, r: float) -> FusedAssessment:
    m = Mass(2 * r - 1, 0.0, 2 - 2 * r) if r >= 0.5 else Mass(0.0, 1 - 2 * r, 2 * r)
    return FusedAssessment(tx, 1, m, m.f, m.f + m.t, m.t, 0.0, False, ())


def endpoints(i: int) -> tuple[str, str]:
    return ACCOUNTS[i % 5], ACCOUNTS[(3 * i + 1) % 5]


# --- Case_Manager op sequences ---------------------------------------------------------

risks = st.sampled_from([0.3, 0.59, 0.6, 0.65, 0.7, 0.8, 0.9, 1.0])
case_ops = st.lists(
    st.one_of(
        st.tuples(st.just("event"), st.integers(0, N_TX - 1), risks, st.integers(1, 5000), st.integers(0, 2)),
        st.tuples(st.just("motif"), st.frozensets(st.integers(0, N_TX - 1), min_size=1, max_size=5),
                  st.sampled_from(list(MotifType)), st.integers(0, 2)),
        st.tuples(st.just("open"), st.integers(1, 8), st.sampled_from(["i1", "i2"]), st.just(0)),
        st.tuples(st.just("label"), st.integers(1, 8), st.sampled_from(["i1", "i2"]), st.just(0)),
        st.tuples(st.just("escalate"), st.integers(1, 8), st.just("i1"), st.just(0)),
    ),
    max_size=30,
)


def build_manager(budget: int = 3, cost: float = 100.0):
    clock = ManualClock(T0)
    pr = Prioritiser(PrioritiserConfig(alert_budget=budget, investigation_cost=cost, period_s=60), clock)
    return clock, pr, CaseManager(RiskConfig(), pr, clock)


def make_event(i: int, amount: int, clock: ManualClock) -> TransactionEvent:
    s, d = endpoints(i)
    return TransactionEvent.model_validate(
        {"tx_id": f"t{i}", "ts": clock.now().isoformat(), "src": s, "dst": d,
         "amount": f"{amount}.00", "currency": "USD"})


def make_motif(idx: frozenset[int], mtype: MotifType) -> MotifInstance:
    edges = tuple(Edge(f"t{i}", *endpoints(i), T0, Decimal(10 * (i + 1)), "USD") for i in sorted(idx))
    accounts = tuple(dict.fromkeys(a for e in edges for a in (e.src, e.dst)))
    txs = frozenset(e.tx_id for e in edges)
    return MotifInstance(f"{mtype.value}:{sorted(txs)}", mtype, txs, accounts, T0, T0, motif_mass(0.5), False, edges)


def membership(cm: CaseManager) -> dict[str, frozenset[str]]:
    return {c.case_id: frozenset(c.tx_ids) for c in cm.cases.values() if c.is_open}


def run_case_ops(ops, check) -> None:
    """Apply ops; ``check(op, before, cm, result)`` runs after every op with a pre-op snapshot."""
    clock, pr, cm = build_manager()
    for op in ops:
        kind = op[0]
        clock.advance(timedelta(seconds=op[-1]))
        before = {
            "membership": membership(cm),
            "tx_to_case": dict(cm.tx_to_case),
            "cases": {cid: (c.created_at, c.seq_no, set(c.tx_ids), set(c.accounts), dict(c.assessments),
                            dict(c.motifs), c.updated_at, c.update_no, set(c.accounts))
                      for cid, c in cm.cases.items() if c.is_open},
            "n_cases": len(cm.cases),
        }
        result = None
        if kind == "event":
            _, i, r, amount, _ = op
            result = cm.on_scored_event(make_event(i, amount, clock), fa(f"t{i}", r), r)
        elif kind == "motif":
            result = cm.on_motif(make_motif(op[1], op[2]), {f"t{i}": fa(f"t{i}", 0.4) for i in op[1]})
        else:
            cid, inv = f"C-{op[1]}", op[2]
            try:
                if kind == "open":
                    cm.open(cid, inv)
                elif kind == "label":
                    cm.submit_label(cid, inv, Label.FALSE_POSITIVE, "n")
                else:
                    cm.escalate(cid, inv)
            except CaseError:
                pass
        check(op, before, cm, result)


def assert_one_open_case_per_event(cm: CaseManager) -> None:
    owner: dict[str, str] = {}
    for c in cm.cases.values():
        if not c.is_open:
            continue
        for tx in c.tx_ids:
            assert tx not in owner, f"{tx} in {owner[tx]} and {c.case_id}"
            owner[tx] = c.case_id
    assert owner == cm.tx_to_case


# Feature: helix-fraud-immune-system, Property 28: Alert routing
@pytest.mark.property
# **Validates: Requirements 13.4, 13.5, 13.6**
@given(ops=case_ops)
def test_alert_routing(ops) -> None:
    def check(op, before, cm, result):
        if op[0] != "event":
            return
        _, i, r, _, _ = op
        tx, (src, _) = f"t{i}", endpoints(i)
        if r < RiskConfig().alert_threshold:
            assert result is None and membership(cm) == before["membership"]
            return
        if tx in before["tx_to_case"]:  # already grouped by a Motif: stays in its Case
            assert result.case_id == before["tx_to_case"][tx]
            return
        holders = [(snap[6], snap[7], cid) for cid, snap in before["cases"].items() if src in snap[3]]
        if holders:
            assert result.case_id == max(holders)[2] and result.action == "add"
        else:
            assert result.action == "create" and len(cm.cases) == before["n_cases"] + 1
        assert tx in cm.get(result.case_id).tx_ids

    run_case_ops(ops, check)


# Feature: helix-fraud-immune-system, Property 42: At most one open Case per event
@pytest.mark.property
# **Validates: Requirements 12.1, 12.3, 12.6**
@given(ops=case_ops)
def test_at_most_one_open_case_per_event(ops) -> None:
    run_case_ops(ops, lambda op, before, cm, result: assert_one_open_case_per_event(cm))


# Feature: helix-fraud-immune-system, Property 43: Merge survivor rule
@pytest.mark.property
# **Validates: Requirements 12.4, 12.5**
@given(ops=case_ops)
def test_merge_survivor_rule(ops) -> None:
    def check(op, before, cm, result):
        if op[0] != "motif":
            return
        hits = {before["tx_to_case"][f"t{i}"] for i in op[1] if f"t{i}" in before["tx_to_case"]}
        if len(hits) < 2:
            return
        survivor = min(hits, key=lambda cid: before["cases"][cid][:2])
        assert result.case_id == survivor and cm.get(survivor).is_open
        queued = set(cm.prioritiser.active_ids()) | set(cm.prioritiser.backlog_ids())
        for cid in hits - {survivor}:
            c = cm.get(cid)
            assert c.status is CaseStatus.CLOSED and c.merged_into == survivor
            assert cid not in queued and c.placement is Placement.NONE

    run_case_ops(ops, check)


# Feature: helix-fraud-immune-system, Property 44: Merge union without duplicates
@pytest.mark.property
# **Validates: Requirements 12.2, 15.1, 15.2, 15.3**
@given(ops=case_ops)
def test_merge_union_without_duplicates(ops) -> None:
    # Evidence_Chain entries are built by task 7.2 from the Case's per-tx assessments and
    # attached Motifs; this checks the union of those contributor references.
    def check(op, before, cm, result):
        if op[0] != "motif":
            return
        inst = make_motif(op[1], op[2])
        hits = {before["tx_to_case"][t] for t in inst.tx_ids if t in before["tx_to_case"]}
        c = cm.get(result.case_id)
        if not hits:
            assert c.tx_ids == set(inst.tx_ids) and set(c.motifs) == {inst.motif_id}
            return
        snaps = [before["cases"][h] for h in hits]
        assert c.tx_ids == set().union(*(s[2] for s in snaps)) | inst.tx_ids
        edge_accounts = {a for e in inst.edges for a in (e.src, e.dst)}
        assert c.accounts == set().union(*(s[3] for s in snaps)) | set(inst.accounts) | edge_accounts
        assert set(c.motifs) == set().union(*(s[5] for s in snaps)) | {inst.motif_id}
        for s in snaps:  # earlier entries unchanged
            for tx, prior in s[4].items():
                if tx not in inst.tx_ids:
                    assert c.assessments[tx] is prior
            for mid, m in s[5].items():
                assert c.motifs[mid] is m
        assert_one_open_case_per_event(cm)

    run_case_ops(ops, check)


# --- Prioritiser op sequences -------------------------------------------------------------

prio_ops = st.lists(
    st.one_of(
        st.tuples(st.just("new"), st.integers(0, 120)),
        st.tuples(st.just("change"), st.integers(0, 30), st.integers(0, 120)),
        st.tuples(st.just("open"), st.integers(0, 30)),
        st.tuples(st.just("remove"), st.integers(0, 30)),
        st.tuples(st.just("tick"), st.integers(0, 150)),
        st.tuples(st.just("req"), st.sampled_from(["alert_budget", "investigation_cost"]),
                  st.sampled_from([0, 1, 2, 3, 5, 1001, -1.0, 0.0, 20.0, 60.0, 2e9])),
    ),
    max_size=50,
)


class PrioDriver:
    """Drives a Prioritiser and keeps an independent admission ledger from its moves."""

    def __init__(self, budget: int, cost: float) -> None:
        self.clock = ManualClock(T0)
        self.pr = Prioritiser(PrioritiserConfig(alert_budget=budget, investigation_cost=cost, period_s=60), self.clock)
        self.keys: list[CaseKey] = []
        self.removed: set[str] = set()
        self.admitted_in: dict[str, int] = {}
        self.ledger: dict[int, int] = {}
        self.budget_of: dict[int, int] = {0: budget}

    def live(self, idx: int) -> CaseKey | None:
        if not self.keys:
            return None
        k = self.keys[idx % len(self.keys)]
        return None if k.case_id in self.removed else k

    def absorb_moves(self) -> list:
        moves = self.pr.drain_moves()
        for mv in moves:
            if mv.new is Placement.ACTIVE:
                self.admitted_in[mv.case_id] = mv.period
                self.ledger[mv.period] = self.ledger.get(mv.period, 0) + 1
            elif mv.old is Placement.ACTIVE and mv.new is Placement.BACKLOG:
                p = self.admitted_in[mv.case_id]
                if p == mv.period:
                    self.ledger[p] -= 1
        self.budget_of.setdefault(self.pr.current_period, self.pr.alert_budget)
        return moves

    def state(self) -> dict[str, tuple]:
        return {cid: (self.pr.placement_of(cid), self.pr.priority_of(cid)) for cid in self.pr.active_ids() + self.pr.backlog_ids()}


def sort_key(d: PrioDriver, cid: str):
    k = next(k for k in d.keys if k.case_id == cid)
    return (-d.pr.priority_of(cid), k.created_at, k.seq_no)


def run_prio_ops(ops, budget, cost, check) -> None:
    d = PrioDriver(budget, cost)
    for op in ops:
        pre = {
            "admitted_now": d.pr.admitted_now, "budget": d.pr.alert_budget, "period": d.pr.current_period,
            "active": [(cid, d.pr.priority_of(cid), d.pr.admitted_period_of(cid),
                        next(k for k in d.keys if k.case_id == cid)) for cid in d.pr.active_ids()],
            "backlog": d.pr.backlog_ids(), "pending": d.pr.pending(), "state": d.state(),
            "cost": d.pr.investigation_cost,
        }
        info: dict = {}
        if op[0] == "new":
            n = len(d.keys) + 1
            k = CaseKey(f"C-{n}", n, d.clock.now(), Decimal(op[1]))
            d.keys.append(k)
            info["placement"] = d.pr.upsert(k)
            info["key"] = k
        elif op[0] == "change":
            k = d.live(op[1])
            if k is not None:
                k2 = CaseKey(k.case_id, k.seq_no, k.created_at, Decimal(op[2]), k.opened)
                d.keys[d.keys.index(k)] = k2
                info["placement"] = d.pr.upsert(k2)
                info["key"] = k2
                info["was"] = pre["state"].get(k2.case_id, (Placement.NONE,))[0]
        elif op[0] == "open":
            k = d.live(op[1])
            if k is not None:
                d.pr.mark_opened(k.case_id)
                d.keys[d.keys.index(k)] = CaseKey(k.case_id, k.seq_no, k.created_at, k.expected_loss, True)
        elif op[0] == "remove":
            k = d.live(op[1])
            if k is not None:
                d.pr.remove(k.case_id)
                d.removed.add(k.case_id)
        elif op[0] == "tick":
            d.clock.advance(timedelta(seconds=op[1]))
            d.pr.on_tick(d.clock.now())
        else:
            try:
                d.pr.request_change(op[1], op[2])
                info["accepted"] = True
            except PrioritiserChangeError:
                info["accepted"] = False
        info["moves"] = d.absorb_moves()
        check(op, pre, d, info)


# Feature: helix-fraud-immune-system, Property 45: Alert budget invariant
@pytest.mark.property
# **Validates: Requirements 14.3, 14.4**
@given(ops=prio_ops, budget=st.integers(1, 4), cost=st.sampled_from([0.0, 20.0, 50.0]))
def test_alert_budget_invariant(ops, budget, cost) -> None:
    def check(op, pre, d, info):
        for p, n in d.ledger.items():
            assert 0 <= n <= d.budget_of[p], (p, n, d.budget_of[p])
        cur = d.pr.current_period
        assert d.ledger.get(cur, 0) == d.pr.admitted_now
        in_period = [c for c in d.pr.active_ids() if d.pr.admitted_period_of(c) == cur]
        assert len(in_period) <= d.pr.alert_budget

    run_prio_ops(ops, budget, cost, check)


# Feature: helix-fraud-immune-system, Property 46: Queue ordering
@pytest.mark.property
# **Validates: Requirements 14.1**
@given(ops=prio_ops, budget=st.integers(1, 4), cost=st.sampled_from([0.0, 20.0, 50.0]))
def test_queue_ordering(ops, budget, cost) -> None:
    def check(op, pre, d, info):
        for q in (d.pr.active_ids(), d.pr.backlog_ids()):
            keys = [sort_key(d, c) for c in q]
            assert keys == sorted(keys)
        assert all(d.pr.priority_of(c) > 0 for c in d.pr.active_ids())

    run_prio_ops(ops, budget, cost, check)


# Feature: helix-fraud-immune-system, Property 47: Admission and displacement
@pytest.mark.property
# **Validates: Requirements 14.5, 14.6, 14.7, 14.8**
@given(ops=prio_ops, budget=st.integers(1, 4), cost=st.sampled_from([0.0, 20.0, 50.0]))
def test_admission_and_displacement(ops, budget, cost) -> None:
    def check(op, pre, d, info):
        if "key" not in info or info.get("was") is Placement.ACTIVE:
            return  # only arrivals (new Cases and Backlog Cases being re-ranked) go through admission
        k: CaseKey = info["key"]
        prio = k.expected_loss - pre["cost"]
        displaced = [m.case_id for m in info["moves"] if m.old is Placement.ACTIVE and m.new is Placement.BACKLOG]
        if prio <= 0:
            assert info["placement"] is Placement.BACKLOG and not displaced
        elif pre["admitted_now"] < pre["budget"]:
            assert info["placement"] is Placement.ACTIVE and not displaced
        else:
            cands = [(p, a) for (cid, p, ap, a) in pre["active"]
                     if ap == pre["period"] and not a.opened and cid != k.case_id]
            victim = min(cands, key=lambda c: (c[0], -c[1].created_at.timestamp(), -c[1].seq_no), default=None)
            if victim is not None and victim[0] < prio:
                assert info["placement"] is Placement.ACTIVE and displaced == [victim[1].case_id]
            else:
                assert info["placement"] is Placement.BACKLOG and not displaced

    run_prio_ops(ops, budget, cost, check)


# Feature: helix-fraud-immune-system, Property 48: Period refill and pending changes
@pytest.mark.property
# **Validates: Requirements 14.9, 14.10, 14.11**
@given(ops=prio_ops, budget=st.integers(1, 4), cost=st.sampled_from([0.0, 20.0, 50.0]))
def test_period_refill_and_pending_changes(ops, budget, cost) -> None:
    def check(op, pre, d, info):
        if op[0] == "req":
            assert d.state() == pre["state"]  # never alters the current queues
            if info["accepted"]:
                assert d.pr.pending()[op[1]] == op[2]
            else:
                assert d.pr.pending() == pre["pending"]
            return
        if op[0] != "tick" or d.pr.current_period == pre["period"]:
            return
        new_budget = pre["pending"].get("alert_budget", pre["budget"])
        new_cost = Decimal(repr(float(pre["pending"]["investigation_cost"]))) if "investigation_cost" in pre["pending"] else pre["cost"]
        assert d.pr.alert_budget == new_budget and d.pr.investigation_cost == new_cost and d.pr.pending() == {}
        live = [k for k in d.keys if k.case_id in pre["state"]]
        pool = sorted(
            (k for k in live if pre["state"][k.case_id][0] is Placement.BACKLOG and k.expected_loss - new_cost > 0),
            key=lambda k: (-(k.expected_loss - new_cost), k.created_at, k.seq_no),
        )
        expected = [k.case_id for k in pool[:new_budget]]
        admitted = [m.case_id for m in info["moves"] if m.new is Placement.ACTIVE]
        assert admitted == expected

    run_prio_ops(ops, budget, cost, check)


# --- Workflow state machine ----------------------------------------------------------------

commands = st.lists(
    st.one_of(
        st.tuples(st.just("open"), st.sampled_from(["i1", "i2"])),
        st.tuples(st.just("label"), st.sampled_from(["i1", "i2"]),
                  st.sampled_from([Label.CONFIRMED_FRAUD, Label.FALSE_POSITIVE, "FALSE_POSITIVE", "BAD", None]),
                  st.sampled_from([0, 7, MAX_NOTE_CHARS, MAX_NOTE_CHARS + 1])),
        st.tuples(st.just("escalate"), st.sampled_from(["i1", "i2"])),
    ),
    max_size=12,
)


def snapshot(c):
    return (c.status, c.assignee, c.tier, c.escalated, c.label, c.label_note, c.placement, len(c.history))


# Feature: helix-fraud-immune-system, Property 56: Workflow state machine
@pytest.mark.property
# **Validates: Requirements 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7, 18.8**
@given(cmds=commands, r=st.sampled_from([0.6, 0.75, 0.85]), steps=st.lists(st.integers(0, 3), min_size=12, max_size=12))
def test_workflow_state_machine(cmds, r, steps) -> None:
    clock, pr, cm = build_manager(budget=5, cost=0.0)
    cm.on_scored_event(make_event(0, 100, clock), fa("t0", r), r)
    case = cm.get("C-1")
    status, assignee, tier = CaseStatus.OPEN, None, case.tier
    for cmd, dt in zip(cmds, steps):
        clock.advance(timedelta(seconds=dt))
        prefix = list(case.history)
        before = snapshot(case)
        if cmd[0] == "open":
            ok = status is CaseStatus.OPEN or (status is CaseStatus.IN_REVIEW and assignee == cmd[1])
            call = lambda: cm.open("C-1", cmd[1])  # noqa: E731
        elif cmd[0] == "label":
            _, by, lab, n = cmd
            ok = (lab in ("CONFIRMED_FRAUD", "FALSE_POSITIVE") and n <= MAX_NOTE_CHARS
                  and status is CaseStatus.IN_REVIEW and by == assignee)
            call = lambda: cm.submit_label("C-1", by, lab, "x" * n)  # noqa: E731
        else:
            ok = status is not CaseStatus.CLOSED
            call = lambda: cm.escalate("C-1", cmd[1])  # noqa: E731
        if ok:
            call()
            if cmd[0] == "open" and status is CaseStatus.OPEN:
                status, assignee = CaseStatus.IN_REVIEW, cmd[1]
            elif cmd[0] == "label":
                status = CaseStatus.CLOSED
                assert case.label == Label(cmd[2]) and case.label_by == cmd[1]
                assert case.placement is Placement.NONE
            elif cmd[0] == "escalate":
                tier = SeverityTier.CRITICAL
        else:
            with pytest.raises(CaseError) as ei:
                call()
            if cmd[0] != "label" or (cmd[2] in ("CONFIRMED_FRAUD", "FALSE_POSITIVE") and cmd[3] <= MAX_NOTE_CHARS):
                assert isinstance(ei.value, CaseConflict)
            assert snapshot(case) == before
        assert (case.status, case.assignee, case.tier) == (status, assignee, tier)
        assert case.history[: len(prefix)] == prefix  # append-only
        ts = [h.ts for h in case.history]
        assert ts == sorted(ts)
