"""Unit tests for helix.cases: Case_Manager and Prioritiser (Requirements 12, 13, 14, 18)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from helix.cases import (
    MAX_NOTE_CHARS,
    CaseConflict,
    CaseKey,
    CaseManager,
    CaseNotFound,
    CaseStatus,
    CaseValidationError,
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
from helix.risk import tier
from helix.sim.clock import ManualClock

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def fa(tx: str, r: float) -> FusedAssessment:
    """Assessment whose pignistic risk is exactly ``r``."""
    m = Mass(2 * r - 1, 0.0, 2 - 2 * r) if r >= 0.5 else Mass(0.0, 1 - 2 * r, 2 * r)
    return FusedAssessment(tx, 1, m, m.f, m.f + m.t, m.t, 0.0, False, ())


def ev(tx: str, src: str, dst: str, amount: str = "100.00", ts: datetime = T0) -> TransactionEvent:
    return TransactionEvent.model_validate(
        {"tx_id": tx, "ts": ts.isoformat(), "src": src, "dst": dst, "amount": amount, "currency": "USD"}
    )


def motif(txs: dict[str, tuple[str, str, str]], mtype: MotifType = MotifType.MULE_CHAIN, m: float = 0.5,
          mid: str | None = None) -> MotifInstance:
    edges = tuple(Edge(t, s, d, T0, Decimal(a), "USD") for t, (s, d, a) in sorted(txs.items()))
    accounts = tuple(dict.fromkeys(a for e in edges for a in (e.src, e.dst)))
    return MotifInstance(mid or f"{mtype.value}:{'-'.join(sorted(txs))}", mtype, frozenset(txs), accounts,
                         T0, T0, motif_mass(m), False, edges)


def make(budget: int = 20, cost: float = 0.0, period_s: int = 3600, on_high=None):
    clock = ManualClock(T0)
    pr = Prioritiser(PrioritiserConfig(alert_budget=budget, investigation_cost=cost, period_s=period_s), clock)
    cm = CaseManager(RiskConfig(), pr, clock, on_high=on_high)
    return clock, pr, cm


def key(cid: int, el: str, created: datetime = T0, opened: bool = False) -> CaseKey:
    return CaseKey(f"C-{cid}", cid, created, Decimal(el), opened)


# --- Prioritiser ---------------------------------------------------------------------


class TestPrioritiser:
    def test_orders_by_priority_desc_then_created_asc(self) -> None:
        clock = ManualClock(T0)
        pr = Prioritiser(PrioritiserConfig(alert_budget=10, investigation_cost=0), clock)
        pr.upsert(key(1, "50", T0 + timedelta(seconds=2)))
        pr.upsert(key(2, "90", T0 + timedelta(seconds=3)))
        pr.upsert(key(3, "50", T0 + timedelta(seconds=1)))
        assert pr.active_ids() == ["C-2", "C-3", "C-1"]

    def test_nonpositive_priority_goes_to_backlog(self) -> None:
        pr = Prioritiser(PrioritiserConfig(alert_budget=5, investigation_cost=50), ManualClock(T0))
        assert pr.upsert(key(1, "50")) is Placement.BACKLOG  # priority 0
        assert pr.upsert(key(2, "50.01")) is Placement.ACTIVE
        assert pr.upsert(key(2, "10")) is Placement.BACKLOG  # Active Case falls to <= 0
        assert pr.admitted_now == 0

    def test_displacement_strictly_lower_later_created_among_ties(self) -> None:
        pr = Prioritiser(PrioritiserConfig(alert_budget=2, investigation_cost=0), ManualClock(T0))
        pr.upsert(key(1, "10", T0))
        pr.upsert(key(2, "10", T0 + timedelta(seconds=1)))
        assert pr.upsert(key(3, "10")) is Placement.BACKLOG  # equal priority: no displacement
        assert pr.upsert(key(4, "11")) is Placement.ACTIVE
        assert pr.placement_of("C-2") is Placement.BACKLOG  # later-created of the tied pair
        assert pr.placement_of("C-1") is Placement.ACTIVE
        assert pr.admitted_now == 2

    def test_opened_cases_are_not_displaced(self) -> None:
        pr = Prioritiser(PrioritiserConfig(alert_budget=1, investigation_cost=0), ManualClock(T0))
        pr.upsert(key(1, "10"))
        pr.mark_opened("C-1")
        assert pr.upsert(key(2, "999")) is Placement.BACKLOG

    def test_previous_period_cases_are_not_displaced(self) -> None:
        clock = ManualClock(T0)
        pr = Prioritiser(PrioritiserConfig(alert_budget=1, investigation_cost=0, period_s=60), clock)
        pr.upsert(key(1, "10"))
        clock.advance(timedelta(seconds=60))
        assert pr.upsert(key(2, "5")) is Placement.ACTIVE  # new period: fresh budget
        assert pr.upsert(key(3, "7")) is Placement.ACTIVE  # displaces C-2, not C-1
        assert pr.placement_of("C-1") is Placement.ACTIVE
        assert pr.placement_of("C-2") is Placement.BACKLOG

    def test_refill_and_pending_changes_apply_next_period(self) -> None:
        clock = ManualClock(T0)
        pr = Prioritiser(PrioritiserConfig(alert_budget=1, investigation_cost=0, period_s=60), clock)
        reports = []
        pr.period_hooks.append(reports.append)
        for i, el in enumerate(["10", "30", "20", "0"], start=1):
            pr.upsert(key(i, el))
        assert pr.active_ids() == ["C-2"] and pr.backlog_ids() == ["C-3", "C-1", "C-4"]
        pr.request_change("alert_budget", 5)
        pr.request_change("investigation_cost", 15.0)
        assert pr.alert_budget == 1 and pr.active_ids() == ["C-2"]  # current queues unchanged
        clock.advance(timedelta(seconds=61))
        pr.on_tick(clock.now())
        assert pr.alert_budget == 5 and pr.investigation_cost == Decimal("15.0")
        # priorities are now EL - 15: C-3=5 admitted; C-1=-5 and C-4=-15 stay in the Backlog
        assert pr.active_ids() == ["C-2", "C-3"] and pr.backlog_ids() == ["C-1", "C-4"]
        assert [(c.name, c.old, c.new) for c in pr.change_log] == [
            ("alert_budget", 1, 5), ("investigation_cost", 0.0, 15.0)]
        assert reports[0].period == 0 and reports[0].admitted == 1 and reports[0].backlog_size == 3

    @pytest.mark.parametrize("name,value", [("alert_budget", 0), ("alert_budget", 1001), ("alert_budget", 2.5),
                                            ("investigation_cost", -1.0), ("investigation_cost", 2e9),
                                            ("period_s", 10)])
    def test_out_of_range_change_rejected(self, name: str, value: float) -> None:
        pr = Prioritiser(PrioritiserConfig(), ManualClock(T0))
        with pytest.raises(PrioritiserChangeError) as ei:
            pr.request_change(name, value)
        assert "must" in ei.value.rule
        assert pr.pending() == {} and pr.alert_budget == 20

    def test_remove_keeps_admission_count(self) -> None:
        pr = Prioritiser(PrioritiserConfig(alert_budget=1, investigation_cost=0), ManualClock(T0))
        pr.upsert(key(1, "10"))
        pr.remove("C-1")
        assert pr.placement_of("C-1") is Placement.NONE
        assert pr.upsert(key(2, "5")) is Placement.BACKLOG  # the period's one slot is spent


# --- Case_Manager: routing, grouping, recompute ---------------------------------------


class TestRouting:
    def test_below_threshold_leaves_membership_unchanged(self) -> None:
        _, _, cm = make()
        assert cm.on_scored_event(ev("t1", "A", "B"), fa("t1", 0.59), 0.59) is None
        assert cm.cases == {}

    def test_create_then_add_to_most_recently_updated(self) -> None:
        clock, _, cm = make()
        c1 = cm.on_scored_event(ev("t1", "A", "B"), fa("t1", 0.7), 0.7)
        assert c1 is not None and c1.action == "create" and c1.case_id == "C-1"
        clock.advance(timedelta(seconds=1))
        cm.on_scored_event(ev("t2", "X", "A"), fa("t2", 0.7), 0.7)  # C-2 contains A as dst
        clock.advance(timedelta(seconds=1))
        ch = cm.on_scored_event(ev("t3", "A", "Z"), fa("t3", 0.65), 0.65)
        assert ch.case_id == "C-2" and ch.action == "add"
        assert cm.get("C-2").tx_ids == {"t2", "t3"}

    def test_risk_expected_loss_priority_and_tier(self) -> None:
        _, _, cm = make(cost=50.0)
        cm.on_scored_event(ev("t1", "A", "B", "1000.00"), fa("t1", 0.75), 0.75)
        c = cm.get("C-1")
        assert c.risk == pytest.approx(0.75) and c.risk_at_creation == c.risk
        assert c.expected_loss == Decimal("750.00") and c.priority == Decimal("700.00")
        assert c.tier is SeverityTier.MEDIUM and c.placement is Placement.ACTIVE
        cm.on_scored_event(ev("t2", "A", "C", "500.00"), fa("t2", 0.95), 0.95)
        assert c.tier is SeverityTier.CRITICAL and c.expected_loss == Decimal("1425.00")
        assert [h.action for h in c.history] == ["create", "placement", "add", "tier"]

    def test_motif_attaches_and_raises_risk(self) -> None:
        _, _, cm = make()
        cm.on_scored_event(ev("t1", "A", "B"), fa("t1", 0.65), 0.65)
        inst = motif({"t1": ("A", "B", "100"), "t2": ("B", "C", "95")}, m=0.6)
        ch = cm.on_motif(inst, {"t2": fa("t2", 0.3)})
        c = cm.get("C-1")
        assert ch.action == "add" and c.tx_ids == {"t1", "t2"} and c.accounts == {"A", "B", "C"}
        assert set(c.motifs) == {inst.motif_id} and c.case_risk.stage2 is not None
        assert c.risk > 0.65 and c.tier is tier(c.risk, RiskConfig().bands)

    def test_same_type_motif_instances_count_once(self) -> None:
        """Three overlapping MULE_CHAIN instances (two sub-chains + the full chain) give the
        same Case risk as the full chain alone; all three stay on the Case."""
        from helix.explain import EntryKind, EvidenceChain

        legs = {"t1": ("A", "B", "100"), "t2": ("B", "C", "95"), "t3": ("C", "D", "90")}
        full = motif(legs, m=0.6)
        subs = [motif({k: legs[k] for k in ("t1", "t2")}, m=0.6), motif({k: legs[k] for k in ("t2", "t3")}, m=0.6)]
        fas = {t: fa(t, 0.4) for t in legs}
        risks = []
        for insts in ([full], [*subs, full]):
            _, _, cm = make()
            cm.on_scored_event(ev("t1", "A", "B"), fa("t1", 0.65), 0.65)
            for inst in insts:
                cm.on_motif(inst, fas)
            c = cm.get("C-1")
            assert len(c.motifs) == len(insts)
            assert [x.contributor_id for x in c.case_risk.stage2.contributions].count("motif:MULE_CHAIN") == 1
            chain = EvidenceChain.build(c)
            assert len(chain.of_kind(EntryKind.MOTIF)) == len(insts)  # all kept for explanation
            (s2,) = [e for e in chain.of_kind(EntryKind.FUSED) if e.detail.get("stage") == 2]
            assert s2.detail["motifs"] == min(i.motif_id for i in insts)  # equal mass: lowest id
            # stage-2 reproduces from the chain (stage-1 fakes here carry no contributions)
            assert [p for p in chain.verify() if p.startswith(repr(s2.key))] == []
            re2 = chain.recompute()[s2.key].mass
            assert (re2.f, re2.l, re2.t) == pytest.approx((s2.mass.f, s2.mass.l, s2.mass.t), abs=1e-9)
            risks.append(c.risk)
        assert risks[1] == pytest.approx(risks[0], abs=1e-12)

    def test_same_type_motif_selection_prefers_highest_fraud_mass(self) -> None:
        from helix.cases.manager import stage2_motifs

        a = motif({"t1": ("A", "B", "1")}, m=0.5, mid="m-a")
        b = motif({"t2": ("B", "C", "1")}, m=0.8, mid="m-b")
        c = motif({"t3": ("C", "D", "1")}, MotifType.FAN_OUT, m=0.3, mid="m-c")
        assert [mid for mid, _ in stage2_motifs({"m-a": a, "m-b": b, "m-c": c})] == ["m-b", "m-c"]

    def test_motif_without_hits_creates_case(self) -> None:
        _, _, cm = make()
        ch = cm.on_motif(motif({"t1": ("A", "B", "100"), "t2": ("B", "C", "95")}),
                         {"t1": fa("t1", 0.4), "t2": fa("t2", 0.45)})
        c = cm.get(ch.case_id)
        assert ch.action == "create" and c.tx_ids == {"t1", "t2"} and c.status is CaseStatus.OPEN

    def test_merge_survivor_and_losers_closed(self) -> None:
        clock, pr, cm = make()
        cm.on_scored_event(ev("t1", "A", "B"), fa("t1", 0.7), 0.7)
        clock.advance(timedelta(seconds=1))
        cm.on_scored_event(ev("t2", "C", "D"), fa("t2", 0.7), 0.7)
        cm.on_scored_event(ev("t3", "E", "F"), fa("t3", 0.7), 0.7)
        ch = cm.on_motif(motif({"t1": ("A", "B", "100"), "t2": ("C", "D", "100"), "t3": ("E", "F", "100"),
                                "t4": ("F", "G", "50")}))
        assert ch.case_id == "C-1" and ch.action == "merge" and ch.merged_from == ("C-2", "C-3")
        s = cm.get("C-1")
        assert s.tx_ids == {"t1", "t2", "t3", "t4"} and s.accounts == set("ABCDEFG")
        for cid in ("C-2", "C-3"):
            loser = cm.get(cid)
            assert loser.status is CaseStatus.CLOSED and loser.merged_into == "C-1"
            assert loser.placement is Placement.NONE and cid not in pr.active_ids() + pr.backlog_ids()
        assert all(cm.case_of(t) == "C-1" for t in ("t1", "t2", "t3", "t4"))

    def test_high_tier_calls_withhold_once(self) -> None:
        seen = []
        _, _, cm = make(on_high=lambda c: seen.append(c.case_id))
        cm.on_scored_event(ev("t1", "A", "B"), fa("t1", 0.85), 0.85)
        cm.on_scored_event(ev("t2", "A", "B"), fa("t2", 0.95), 0.95)
        assert seen == ["C-1"]

    def test_changes_are_published(self) -> None:
        _, _, cm = make()
        got = []
        cm.subscribe(got.append)
        cm.on_scored_event(ev("t1", "A", "B"), fa("t1", 0.7), 0.7)
        drained = cm.drain_changes()
        assert drained == got and drained[0].action == "create" and drained[0].placement is Placement.ACTIVE
        assert cm.drain_changes() == []

    def test_tick_applies_cost_change_to_case_priority(self) -> None:
        clock, pr, cm = make(cost=0.0, period_s=60)
        cm.on_scored_event(ev("t1", "A", "B", "100.00"), fa("t1", 0.7), 0.7)
        pr.request_change("investigation_cost", 100.0)
        clock.advance(timedelta(seconds=60))
        changes = cm.tick()
        c = cm.get("C-1")
        assert c.priority == Decimal("-30.00") and c.placement is Placement.BACKLOG
        assert {ch.action for ch in changes} == {"priority", "placement"}


# --- Case_Manager: workflow (Req 18) -----------------------------------------------------


class TestWorkflow:
    def _case(self):
        clock, pr, cm = make()
        cm.on_scored_event(ev("t1", "A", "B"), fa("t1", 0.7), 0.7)
        return clock, pr, cm

    def test_open_label_close(self) -> None:
        clock, pr, cm = self._case()
        clock.advance(timedelta(seconds=5))
        c = cm.open("C-1", "inv1")
        assert c.status is CaseStatus.IN_REVIEW and c.assignee == "inv1" and c.opened_at == clock.now()
        assert c.placement is Placement.ACTIVE
        assert cm.open("C-1", "inv1") is c  # idempotent for the assignee
        clock.advance(timedelta(seconds=5))
        cm.submit_label("C-1", "inv1", "CONFIRMED_FRAUD", "mule ring")
        assert c.status is CaseStatus.CLOSED and c.label is Label.CONFIRMED_FRAUD and c.label_by == "inv1"
        assert c.placement is Placement.NONE and "C-1" not in pr.active_ids()
        assert cm.case_of("t1") is None
        statuses = [(h.prev, h.new, h.actor) for h in c.history if h.action in ("open", "label")]
        assert statuses == [("OPEN", "IN_REVIEW", "inv1"), ("IN_REVIEW", "CLOSED", "inv1")]
        ts = [h.ts for h in c.history]
        assert ts == sorted(ts)

    def test_conflicts_and_validation_leave_case_unchanged(self) -> None:
        _, _, cm = self._case()
        with pytest.raises(CaseConflict):
            cm.submit_label("C-1", "inv1", Label.FALSE_POSITIVE, "")  # not IN_REVIEW
        cm.open("C-1", "inv1")
        before = (cm.get("C-1").status, len(cm.get("C-1").history))
        with pytest.raises(CaseConflict) as ei:
            cm.open("C-1", "inv2")
        assert "inv1" in str(ei.value) and ei.value.assignee == "inv1"
        with pytest.raises(CaseConflict):
            cm.submit_label("C-1", "inv2", Label.FALSE_POSITIVE, "")
        with pytest.raises(CaseValidationError):
            cm.submit_label("C-1", "inv1", "MAYBE", "")
        with pytest.raises(CaseValidationError):
            cm.submit_label("C-1", "inv1", Label.FALSE_POSITIVE, "x" * (MAX_NOTE_CHARS + 1))
        assert (cm.get("C-1").status, len(cm.get("C-1").history)) == before
        cm.submit_label("C-1", "inv1", Label.FALSE_POSITIVE, "x" * MAX_NOTE_CHARS)
        with pytest.raises(CaseConflict):
            cm.open("C-1", "inv1")
        with pytest.raises(CaseConflict):
            cm.escalate("C-1", "inv1")
        with pytest.raises(CaseNotFound):
            cm.open("C-99", "inv1")

    def test_escalate_is_sticky_critical(self) -> None:
        _, _, cm = self._case()
        c = cm.escalate("C-1", "inv3")
        assert c.tier is SeverityTier.CRITICAL and c.status is CaseStatus.OPEN and c.escalated
        h = c.history[-1]
        assert (h.action, h.prev, h.new, h.actor) == ("escalate", "MEDIUM", "CRITICAL", "inv3")
        cm.on_scored_event(ev("t2", "A", "C"), fa("t2", 0.61), 0.61)  # recompute must not downgrade
        assert c.tier is SeverityTier.CRITICAL
