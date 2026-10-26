"""Unit tests for helix.replay (design §15, Requirement 17)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from helix.cases import Case, CaseStatus, Placement
from helix.codec import TransactionEvent
from helix.config import HelixConfig, SeverityTier
from helix.config.schema import GraphConfig, SensorConfig
from helix.fusion import ContributorType
from helix.graph import TemporalGraph
from helix.replay import (
    DefaultStage1Engine,
    ReplayParameterError,
    ReplayService,
    TimestampOutOfRangeError,
    UnknownAccountError,
    UnknownCaseError,
    expand,
    frame_at,
    mismatches,
    recompute,
    related_subgraph,
    replay_timeline,
    weights_at,
)
from helix.storage import Storage

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
CFG = HelixConfig(sensors=SensorConfig(min_history_count=2), graph=GraphConfig(window_s=3_600))


def zero_clock() -> float:
    return 0.0


def ev(i: int, minutes: float, src: str, dst: str, amount: str = "10.00") -> TransactionEvent:
    return TransactionEvent(
        tx_id=f"T{i:03d}", ts=T0 + timedelta(minutes=minutes), src=src, dst=dst,
        amount=Decimal(amount), currency="USD", channel="web",
    )  # fmt: skip


def make_case(accounts, tx_ids, updated_at, case_id="C-1") -> Case:
    return Case(
        case_id=case_id, seq_no=1, status=CaseStatus.OPEN, tier=SeverityTier.HIGH, risk=0.8,
        risk_at_creation=0.8, expected_loss=Decimal("0"), priority=Decimal("0"),
        accounts=set(accounts), tx_ids=set(tx_ids), assignee=None, created_at=updated_at,
        updated_at=updated_at, admitted_period=None, placement=Placement.ACTIVE,
        merged_into=None, label=None, label_note=None, escalated=False, history=[],
    )  # fmt: skip


def run_live(storage: Storage, events, cfg=CFG, snapshot_after: int | None = None):
    """Drive a DefaultStage1Engine like the pipeline and persist events + assessments."""
    eng = DefaultStage1Engine(cfg, clock=zero_clock)
    clock = None
    for seq, e in enumerate(events, start=1):
        fa = eng.process(e)
        storage.record_event(e, seq_no=seq)
        storage.record_assessment(fa, risk=fa.belief)
        clock = e.ts if clock is None else max(clock, e.ts)
        if snapshot_after == seq:
            dna, cpd = eng.snapshot_state(last_seq_no=seq)
            storage.snapshot(dna, cpd, clock)
    assert storage.flush()
    return eng


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "replay.duckdb").open()
    yield s
    s.close()


# ---- Related_Subgraph ---------------------------------------------------------------
def chain_graph() -> TemporalGraph:
    g = TemporalGraph(3_600)
    for i, (s, d) in enumerate([("A", "B"), ("B", "C"), ("C", "D"), ("E", "A")]):
        g.add(ev(i, i, s, d))
    return g


def test_subgraph_hops_flags_and_matches_graph_query():
    g = chain_graph()
    sub = related_subgraph({"A"}, g, hops=2, max_nodes=50)
    assert {(n.account, n.hop, n.is_case_account) for n in sub.nodes} == {
        ("A", 0, True), ("B", 1, False), ("E", 1, False), ("C", 2, False),
    }  # fmt: skip
    assert [e.tx_id for e in sub.edges] == ["T000", "T001", "T003"]
    assert not sub.truncated and sub.omitted == 0
    q = g.query("A", 2, sub.start, sub.end)
    assert sub.accounts == q.nodes and sub.edges == q.edges
    e = sub.edges[0]
    assert (e.tx_id, e.ts, e.amount) == ("T000", T0, Decimal("10.00"))


def test_subgraph_includes_case_account_without_edges():
    sub = related_subgraph({"A", "Z"}, chain_graph(), hops=1, max_nodes=50)
    assert sub.node("Z") is not None and sub.node("Z").is_case_account


def test_subgraph_truncation_keeps_case_accounts_and_nearest():
    g = TemporalGraph(3_600)
    i = 0
    for k in range(60):  # 60 one-hop leaves, each with a two-hop child
        g.add(ev(i, 1, "X", f"L{k:02d}")); i += 1  # noqa: E702
        g.add(ev(i, 2, f"L{k:02d}", f"M{k:02d}")); i += 1  # noqa: E702
    sub = related_subgraph({"X"}, g, hops=2, max_nodes=50)
    assert len(sub.nodes) == 50 and sub.truncated and sub.omitted == 121 - 50
    assert sub.node("X").is_case_account
    assert all(n.hop <= 1 for n in sub.nodes)  # nearest by hop first
    kept = sub.accounts
    assert all(e.src in kept and e.dst in kept for e in sub.edges)


@pytest.mark.parametrize("hops,max_nodes", [(0, 50), (5, 50), (2, 49), (2, 501), (True, 50)])
def test_subgraph_parameter_ranges(hops, max_nodes):
    with pytest.raises(ReplayParameterError):
        related_subgraph({"A"}, chain_graph(), hops, max_nodes)


def test_expand_returns_neighbours_and_rejects_unknown_account():
    g = chain_graph()
    sub = related_subgraph({"A"}, g, hops=1, max_nodes=50)
    nb = expand(sub, g, "B")
    assert [n.account for n in nb.nodes] == ["B", "A", "C"]
    assert nb.nodes[1].is_case_account and not nb.nodes[2].is_case_account
    assert [e.tx_id for e in nb.edges] == ["T000", "T001"]
    with pytest.raises(UnknownAccountError) as ei:
        expand(sub, g, "D")  # in the graph, not in the returned subgraph
    assert ei.value.account == "D"


# ---- timeline / frames ------------------------------------------------------------------
EVENTS = [
    ev(1, 0, "A", "B"), ev(2, 30, "C", "D"), ev(3, 60, "A", "C"), ev(4, 90, "D", "A"),
    ev(5, 120, "A", "B", "500.00"), ev(6, 150, "C", "D"), ev(7, 200, "A", "B"),
]  # fmt: skip


def test_timeline_span_order_and_stored_assessments(storage):
    run_live(storage, EVENTS)
    case = make_case({"A"}, {"T005"}, T0 + timedelta(minutes=150))
    tl = replay_timeline(case, storage, lead_h=1.0)
    assert tl.start == T0 + timedelta(minutes=60) and tl.end == T0 + timedelta(minutes=150)
    assert [e.tx_id for e in tl.entries] == ["T003", "T004", "T005"]  # T004: A as dst
    for e in tl.entries:
        assert e.assessment is not None and e.assessment.tx_id == e.tx_id and e.assessment.stage == 1
        assert len(e.sensor_contributions) == 6
        assert all(c.kind is ContributorType.SENSOR for c in e.sensor_contributions)


def test_timeline_rejects_out_of_range_lead(storage):
    with pytest.raises(ReplayParameterError):
        replay_timeline(make_case({"A"}, set(), T0), storage, lead_h=169)


def test_frame_at_isolates_point_in_time(storage):
    run_live(storage, EVENTS)
    case = make_case({"A"}, {"T005"}, T0 + timedelta(minutes=200))
    t = T0 + timedelta(minutes=125)
    fr = frame_at(case, storage, t, window_s=3_600, hops=2, max_nodes=50, lead_h=2.0)
    assert [e.tx_id for e in fr.entries] == ["T001", "T003", "T004", "T005"]
    # window [65 min, 125 min]: T003 (60) is too old, T006 (150) is in the future
    assert {e.tx_id for e in fr.subgraph.edges} == {"T004", "T005"}
    assert all(t - timedelta(hours=1) <= e.ts <= t for e in fr.subgraph.edges)
    with pytest.raises(TimestampOutOfRangeError) as ei:
        frame_at(case, storage, T0 + timedelta(minutes=201), window_s=3_600, lead_h=2.0)
    assert ei.value.start == T0 and ei.value.end == T0 + timedelta(minutes=200)


# ---- recompute ------------------------------------------------------------------------
def test_recompute_reproduces_from_snapshot(storage):
    run_live(storage, EVENTS, snapshot_after=2)
    case = make_case({"A"}, {"T005", "T007"}, T0 + timedelta(minutes=200))
    run = recompute(case, storage, lambda s, w, c: DefaultStage1Engine.from_snapshot(s, w, c, clock=zero_clock), CFG, lead_h=1.0)
    assert run.snapshot_no == 1
    assert [a.tx_id for a in run.assessments] == ["T003", "T004", "T005", "T007"]
    assert mismatches(run) == []
    assert run.events_fed == 4  # seq > 2 with src in {A, D}: T003, T004, T005, T007


def test_recompute_without_snapshot_starts_empty(storage):
    run_live(storage, EVENTS)
    case = make_case({"C"}, {"T006"}, T0 + timedelta(minutes=150))
    run = recompute(case, storage, lambda s, w, c: DefaultStage1Engine.from_snapshot(s, w, c, clock=zero_clock), CFG, lead_h=0.0)
    assert run.snapshot_no is None and mismatches(run) == []


def test_weights_at_takes_latest_record_at_or_before():
    recs = [
        {"contributor_id": "amount", "new": 0.9, "ts": T0},
        {"contributor_id": "amount", "new": 0.5, "ts": T0 + timedelta(hours=1)},
        {"contributor_id": "geo", "new": 0.7, "ts": T0 + timedelta(hours=2)},
    ]
    assert weights_at(recs, T0 + timedelta(minutes=30)) == {"amount": 0.9}
    assert weights_at(recs, T0 + timedelta(hours=2)) == {"amount": 0.5, "geo": 0.7}


# ---- service --------------------------------------------------------------------------
def test_service_unknown_case_and_round_trip(storage):
    run_live(storage, EVENTS, snapshot_after=2)
    case = make_case({"A"}, {"T005"}, T0 + timedelta(minutes=200))
    svc = ReplayService(
        storage, {"C-1": case}, CFG,
        engine_factory=lambda s, w, c: DefaultStage1Engine.from_snapshot(s, w, c, clock=zero_clock),
    )  # fmt: skip
    with pytest.raises(UnknownCaseError):
        svc.timeline("C-404")
    assert svc.subgraph("C-1").node("A").is_case_account
    assert [e.tx_id for e in svc.timeline("C-1").entries][0] == "T001"  # 24 h default lead
    fr = svc.at("C-1", T0 + timedelta(minutes=100))
    assert all(e.ts <= T0 + timedelta(minutes=100) for e in fr.entries)
    with pytest.raises(UnknownAccountError):
        svc.expand("C-1", "nobody", T0 + timedelta(minutes=100))
    stored = [e.assessment for e in svc.timeline("C-1").entries]
    assert svc.recompute("C-1") == stored
