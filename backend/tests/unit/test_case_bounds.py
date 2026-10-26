"""Bounded Cases and incremental ACT work (task 19.1; Requirements 12, 25).

Case growth is capped by ``risk.case_max_accounts`` / ``case_max_events``; the pipeline extends
Evidence_Chains, kill chains and Storage rows incrementally instead of rebuilding them from the
whole Case on every event."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from helix.cases import CaseManager, CaseStatus, Prioritiser
from helix.config import HelixConfig
from helix.config.schema import PrioritiserConfig, RiskConfig
from helix.config.validation import validate_config
from helix.core.pipeline import Helix
from helix.cases.manager import stage2_motifs
from helix.explain import ChainBuilder, dumps
from helix.motifs import MotifType
from helix.sim.clock import ManualClock
from helix.sim.generator import SyntheticGenerator
from helix.sim.ground_truth import InMemoryGroundTruth
from tests.unit.test_cases import T0, ev, fa, motif

GEN_T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)


def manager(max_accounts: int, max_events: int = 500) -> CaseManager:
    clock = ManualClock(T0)
    pr = Prioritiser(PrioritiserConfig(alert_budget=20, investigation_cost=0), clock)
    return CaseManager(RiskConfig(), pr, clock, max_accounts=max_accounts, max_events=max_events)


def alert(cm: CaseManager, tx: str, src: str, dst: str) -> str:
    cm.on_scored_event(ev(tx, src, dst), fa(tx, 0.9), 0.9)
    return cm.case_of(tx)


def edges(*txs: tuple[str, str, str]) -> dict[str, tuple[str, str, str]]:
    return {t: (s, d, "100.00") for t, s, d in txs}


# --- Case_Manager bounds ------------------------------------------------------------------


def test_bounds_are_validated():
    base = HelixConfig()
    for field, bad in (("case_max_accounts", 1), ("case_max_events", 0), ("case_max_motifs", 0)):
        cfg = base.model_copy(update={"risk": base.risk.model_copy(update={field: bad})})
        assert [e for e in validate_config(cfg) if field in str(e)], field
    with pytest.raises(ValueError):
        manager(max_accounts=1)
    cm = CaseManager(RiskConfig(case_max_accounts=7, case_max_events=9),
                     Prioritiser(PrioritiserConfig(), ManualClock(T0)), ManualClock(T0))
    assert (cm.max_accounts, cm.max_events) == (7, 9)


def test_routing_opens_an_overflow_case_when_every_source_case_is_full():
    cm = manager(max_accounts=3)
    c1 = alert(cm, "t1", "A", "B")
    assert alert(cm, "t2", "A", "C") == c1  # 3 accounts: still fits
    c2 = alert(cm, "t3", "A", "D")  # a 4th account would overflow C-1
    assert c2 != c1 and cm.overflows == 1
    assert cm.get(c1).accounts == {"A", "B", "C"} and cm.get(c2).accounts == {"A", "D"}
    assert ("overflow", c1, c2) in [(h.action, h.prev, h.new) for h in cm.get(c2).history]
    assert all(c.is_open for c in cm.cases.values())


def test_routing_respects_the_event_bound():
    cm = manager(max_accounts=50, max_events=2)
    c1 = alert(cm, "t1", "A", "B")
    assert alert(cm, "t2", "A", "B") == c1
    assert alert(cm, "t3", "A", "B") != c1
    assert len(cm.get(c1).tx_ids) == 2


def test_motif_that_does_not_fit_the_survivor_starts_an_overflow_case():
    cm = manager(max_accounts=4)
    c1 = alert(cm, "t0", "A", "E")
    assert alert(cm, "t1", "A", "B") == c1  # C-1 = {A, B, E}
    c2 = alert(cm, "t2", "C", "D")  # C-2 = {C, D}
    inst = motif(edges(("t1", "A", "B"), ("t3", "B", "C"), ("t2", "C", "D")))
    cm.on_motif(inst)
    c3 = cm.case_of("t3")
    assert c3 not in (c1, c2) and cm.overflows == 1
    # No snowball: both earlier Cases stay open and keep their own events.
    assert cm.get(c1).is_open and cm.get(c2).is_open
    assert cm.get(c1).merged_into is None and cm.get(c2).merged_into is None
    assert (cm.case_of("t1"), cm.case_of("t2")) == (c1, c2)
    assert inst.motif_id in cm.get(c3).motifs and len(cm.get(c3).accounts) <= 4
    assert ("overflow", c1, c3) in [(h.action, h.prev, h.new) for h in cm.get(c3).history]


def test_merge_skips_a_case_that_would_overflow_the_survivor():
    cm = manager(max_accounts=5)
    c1 = alert(cm, "t0", "A", "E")
    alert(cm, "t1", "A", "B")  # C-1 = {A, B, E}
    c2 = alert(cm, "t2", "C", "D")
    assert alert(cm, "t4", "C", "F") == c2  # C-2 = {C, D, F}
    cm.on_motif(motif(edges(("t1", "A", "B"), ("t3", "B", "C"), ("t2", "C", "D"))))
    assert cm.case_of("t3") == c1 and cm.get(c1).accounts == {"A", "B", "C", "D", "E"}
    assert cm.get(c2).is_open and cm.get(c2).tx_ids == {"t2", "t4"}  # unmerged
    assert cm.overflows == 0


def test_merge_within_the_bound_still_happens():
    cm = manager(max_accounts=50)
    c1 = alert(cm, "t1", "A", "B")
    c2 = alert(cm, "t2", "C", "D")
    cm.on_motif(motif(edges(("t1", "A", "B"), ("t3", "B", "C"), ("t2", "C", "D"))))
    assert cm.get(c2).status is CaseStatus.CLOSED and cm.get(c2).merged_into == c1
    assert cm.get(c1).tx_ids == {"t1", "t2", "t3"}


def test_full_survivor_with_nothing_ungrouped_keeps_the_motif_as_evidence_only():
    cm = manager(max_accounts=4)
    c1 = alert(cm, "t0", "A", "E")
    alert(cm, "t1", "A", "B")
    c2 = alert(cm, "t2", "C", "D")
    inst = motif(edges(("t1", "A", "B"), ("t2", "C", "D")), mtype=MotifType.FAN_IN)
    cm.on_motif(inst)
    assert inst.motif_id in cm.get(c1).motifs
    assert cm.get(c1).accounts == {"A", "B", "E"} and cm.get(c2).is_open
    assert len(cm.cases) == 2



def test_motif_cap_keeps_only_instances_that_change_the_stage2_selection():
    cm = manager(max_accounts=50)
    cm.max_motifs = 2
    cid = alert(cm, "t1", "A", "B")
    alert(cm, "t2", "B", "C")
    cm.on_motif(motif(edges(("t1", "A", "B")), mtype=MotifType.FAN_IN, m=0.5, mid="m1"))
    cm.on_motif(motif(edges(("t2", "B", "C")), mtype=MotifType.FAN_OUT, m=0.5, mid="m2"))
    case = cm.get(cid)
    risk, n_hist = case.risk, len(case.history)
    # At the cap, a same-type, not-better instance over held evidence is a no-op.
    ch = cm.on_motif(motif(edges(("t1", "A", "B"), ("t2", "B", "C")), mtype=MotifType.FAN_IN, m=0.4, mid="m3"))
    assert ch.case_id == cid and "m3" not in case.motifs and cm.motifs_dropped == 1
    assert (case.risk, len(case.history)) == (risk, n_hist)
    # A new MotifType (or a better instance of a held type) still enters and moves the risk.
    cm.on_motif(motif(edges(("t1", "A", "B")), mtype=MotifType.MULE_CHAIN, m=0.8, mid="m4"))
    assert "m4" in case.motifs and case.risk > risk
    assert cm.stage2_selection(case) == stage2_motifs(case.motifs)


def test_amount_total_survives_a_rescan():
    cm = manager(max_accounts=50)
    cid = alert(cm, "t1", "A", "B")
    alert(cm, "t2", "A", "C")
    case = cm.get(cid)
    case.amount_n = -1  # e.g. a Case restored from Storage
    assert cm._amount_total(case) == Decimal("200.00")


# --- pipeline: incremental Evidence_Chain, kill chain and persistence ----------------------


def dense_cfg(max_accounts: int = 10, max_motifs: int = 20, soc: bool = False) -> HelixConfig:
    """A small, dense synthetic graph: without the bounds its Cases snowball within seconds."""
    cfg = HelixConfig()
    upd: dict[str, Any] = {
        "generator": cfg.generator.model_copy(update={"accounts": 60}),
        "risk": cfg.risk.model_copy(update={"case_max_accounts": max_accounts, "case_max_motifs": max_motifs}),
    }
    if soc:
        upd["soc"] = cfg.soc.model_copy(update={"enabled": True})
    return cfg.model_copy(update=upd)


class RecordingStorage:
    """Duck-typed Storage that records the Case rows the pipeline writes."""

    def __init__(self) -> None:
        self.case_writes: Counter[str] = Counter()
        self.rows: dict[str, dict] = {}
        self.chains: dict[str, str] = {}
        self.history: list[dict] = []

    def record_event(self, *a: Any, **k: Any) -> None:
        pass

    def record_assessment(self, *a: Any, **k: Any) -> None:
        pass

    def upsert(self, table: str, row: dict) -> None:
        if table == "cases":
            self.case_writes[row["case_id"]] += 1
            self.rows[row["case_id"]] = row
        elif table == "evidence_chains":
            self.chains[row["case_id"]] = row["chain"]

    def append(self, table: str, row: dict) -> None:
        self.history.append(row)


class Run:
    def __init__(self, cfg: HelixConfig, n: int, seed: int = 7, interval: float = 1.0) -> None:
        self.mono = 0.0
        self.st = RecordingStorage()
        self.h = Helix(cfg, ManualClock(GEN_T0), sensor_clock=lambda: 0.0, storage=self.st,
                       case_persist_interval_s=interval, monotonic=lambda: self.mono)
        self.gen = SyntheticGenerator(cfg.generator, ManualClock(GEN_T0), InMemoryGroundTruth(), seed=seed)
        self.outs = [self.step() for _ in range(n)]

    def step(self) -> Any:
        return self.h.process(next(self.gen))


@pytest.fixture(scope="module")
def dense() -> Run:
    """Read-only shared run (tests that mutate it build their own)."""
    return Run(dense_cfg(), 1000)


def test_dense_graph_cases_stay_within_the_bounds(dense: Run):
    h, outs = dense.h, dense.outs
    biggest_inst = max((len(m.accounts) for o in outs for m in o.motifs), default=0)
    open_cases = h.cases.open_cases()
    assert open_cases and sum(len(o.motifs) for o in outs) > 0
    assert max(len(c.accounts) for c in open_cases) <= max(10, biggest_inst)
    assert h.cases.motifs_dropped > 0
    # Each Transaction_Event belongs to at most one open Case (Req 12.6).
    held = Counter(tx for c in open_cases for tx in c.tx_ids)
    assert not [tx for tx, n in held.items() if n > 1]


def test_incremental_chain_holds_every_entry_of_a_full_rebuild(dense: Run):
    h = dense.h
    open_cases = h.cases.open_cases()
    assert open_cases
    for case in open_cases:
        b = ChainBuilder(case.case_id)
        b.add(h._chain_entries(case, sorted(case.tx_ids), sorted(case.motifs)))
        assert {e.key for e in b.entries()} <= {e.key for e in h.chains[case.case_id]}, case.case_id


def test_open_case_writes_are_coalesced_while_the_interval_has_not_elapsed(dense: Run):
    h, st = dense.h, dense.st
    touches = Counter(ch.case_id for o in dense.outs for ch in o.case_changes)
    assert any(n > 1 for n in touches.values())
    # Monotonic clock frozen: an open Case is written once (when first seen) however often
    # it changes; the latest state is pending.
    assert all(st.case_writes[cid] == 1 for cid, c in h.cases.cases.items() if c.is_open)
    assert h._persist_pending


def test_incremental_kill_chain_matches_a_full_reconstruction():
    h = Run(dense_cfg(soc=True), 800).h
    staged = 0
    for case in h.cases.open_cases():
        kc = h.kill_chain(case.case_id)
        assert kc is not None
        assert case.kill_chain_stage is kc.most_advanced, case.case_id
        staged += case.kill_chain_stage is not None
    assert staged > 0


def test_flush_and_due_writes_persist_the_latest_state():
    r = Run(dense_cfg(), 500)
    h, st = r.h, r.st
    pending = set(h._persist_pending)
    assert pending
    before = {cid: st.case_writes[cid] for cid in pending}
    r.mono = 5.0  # interval elapsed: the next event writes every due Case
    r.step()
    assert all(st.case_writes[cid] > before[cid] for cid in pending)
    r.step()
    h.flush_cases()
    assert h.flush_cases() == 0 and not h._persist_pending
    for cid, case in h.cases.cases.items():
        assert st.rows[cid]["tx_ids"] == sorted(case.tx_ids)
        if cid in h.chains:
            assert st.chains[cid] == dumps(h.chains[cid])
    keys = [(row["case_id"], row["entry_no"]) for row in st.history]  # each entry once
    assert len(keys) == len(set(keys)) == sum(len(c.history) for c in h.cases.cases.values())


def test_investigator_actions_are_written_at_once():
    r = Run(dense_cfg(), 300, interval=3600.0)
    cid = r.h.cases.open_cases()[0].case_id
    n = r.st.case_writes[cid]
    r.h.open_case(cid, "ana")
    assert r.st.case_writes[cid] == n + 1 and r.st.rows[cid]["status"] == "IN_REVIEW"
