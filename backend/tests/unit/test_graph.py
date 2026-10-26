"""Unit tests for helix.graph.TemporalGraph (Requirement 10)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from helix.config import HelixConfig
from helix.graph import Edge, GraphQueryError, TemporalGraph

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
HOUR = 3_600.0


@dataclass(frozen=True)
class Ev:
    tx_id: str
    ts: datetime
    src: str
    dst: str
    amount: Decimal = Decimal("10.00")
    currency: str = "USD"


def ev(tx: str, h: float, src: str, dst: str, amount: str = "10.00") -> Ev:
    return Ev(tx, T0 + timedelta(hours=h), src, dst, Decimal(amount))


def snapshot(g: TemporalGraph):
    return (set(g.nodes()), g.edges(), g.latest)


def test_add_creates_nodes_and_keeps_parallel_edges():
    g = TemporalGraph(window_s=24 * HOUR)
    assert g.add(ev("t1", 0, "A", "B", "5.50"))
    assert g.add(ev("t2", 1, "A", "B"))
    assert set(g.nodes()) == {"A", "B"}
    assert g.num_edges == 2 and "t1" in g and "t2" in g
    e = g.out_edges("A")[0]
    assert e == Edge("t1", "A", "B", T0, Decimal("5.50"), "USD")
    assert g.latest == T0 + timedelta(hours=1)


def test_from_config_uses_window():
    g = TemporalGraph.from_config(HelixConfig().graph)
    assert g.window == timedelta(days=7)


def test_eviction_strictly_older_and_boundary_kept():
    g = TemporalGraph(window_s=10 * HOUR)
    g.add(ev("old", 0, "A", "B"))
    g.add(ev("edge", 1, "C", "D"))
    g.add(ev("new", 11, "E", "F"))  # cutoff = 1h: "old" evicted, "edge" (== cutoff) kept
    assert "old" not in g and "edge" in g and "new" in g
    assert not g.has_node("A") and not g.has_node("B")  # degree-0 cleanup
    assert g.has_node("C") and g.has_node("D")


def test_node_with_remaining_edges_survives_eviction():
    g = TemporalGraph(window_s=10 * HOUR)
    g.add(ev("t1", 0, "A", "B"))
    g.add(ev("t2", 5, "A", "C"))
    g.add(ev("t3", 12, "X", "Y"))
    assert g.has_node("A") and g.has_node("C") and not g.has_node("B")


def test_late_event_within_window_accepted_without_moving_latest():
    g = TemporalGraph(window_s=10 * HOUR)
    g.add(ev("t1", 10, "A", "B"))
    assert g.add(ev("late", 0, "B", "C"))  # exactly on the boundary
    assert g.latest == T0 + timedelta(hours=10)
    # Late edge pushed into the heap out of order is still evicted correctly later.
    g.add(ev("t2", 10.5, "D", "E"))
    assert "late" not in g and not g.has_node("C") and "t1" in g


def test_too_old_rejected_without_change():
    g = TemporalGraph(window_s=10 * HOUR)
    g.add(ev("t1", 20, "A", "B"))
    before = snapshot(g)
    assert not g.add(ev("t0", 9.9, "Z", "Q"))
    assert snapshot(g) == before and not g.has_node("Z")


def test_duplicate_rejected_without_change():
    g = TemporalGraph(window_s=10 * HOUR)
    g.add(ev("t1", 0, "A", "B"))
    before = snapshot(g)
    assert not g.add(ev("t1", 5, "C", "D"))
    assert snapshot(g) == before


def test_in_out_edges_time_range_and_order():
    g = TemporalGraph(window_s=100 * HOUR)
    g.add(ev("t3", 3, "A", "B"))
    g.add(ev("t1", 1, "A", "C"))
    g.add(ev("t2", 2, "D", "A"))
    assert [e.tx_id for e in g.out_edges("A")] == ["t1", "t3"]
    assert [e.tx_id for e in g.out_edges("A", T0 + timedelta(hours=2), T0 + timedelta(hours=3))] == ["t3"]
    assert [e.tx_id for e in g.in_edges("A", end=T0 + timedelta(hours=2))] == ["t2"]
    assert g.out_edges("missing") == [] and g.in_edges("missing") == []
    assert {t for t, *_ in g.iter_out_edges("A")} == {"t1", "t3"}
    assert [(t, s) for t, s, *_ in g.iter_in_edges("A")] == [("t2", "D")]


def _chain() -> TemporalGraph:
    # A -> B -> C <- D, with C -> E outside the later time range
    g = TemporalGraph(window_s=100 * HOUR)
    g.add(ev("ab", 1, "A", "B"))
    g.add(ev("bc", 2, "B", "C"))
    g.add(ev("dc", 3, "D", "C"))
    g.add(ev("ce", 9, "C", "E"))
    return g


def test_query_hops_and_both_directions():
    g = _chain()
    lo, hi = T0, T0 + timedelta(hours=10)
    one = g.query("B", 1, lo, hi)
    assert one.nodes == {"A", "B", "C"} and [e.tx_id for e in one.edges] == ["ab", "bc"]
    two = g.query("B", 2, lo, hi)
    assert two.nodes == {"A", "B", "C", "D", "E"}
    assert [e.tx_id for e in two.edges] == ["ab", "bc", "dc", "ce"]


def test_query_time_range_inclusive():
    g = _chain()
    sub = g.query("C", 6, T0 + timedelta(hours=2), T0 + timedelta(hours=3))
    assert sub.nodes == {"B", "C", "D"} and {e.tx_id for e in sub.edges} == {"bc", "dc"}


def test_query_known_account_without_in_range_edges_returns_only_itself():
    g = _chain()
    sub = g.query("E", 3, T0, T0 + timedelta(hours=1))
    assert sub.nodes == {"E"} and sub.edges == ()


def test_query_unknown_account_empty_and_non_mutating():
    g = _chain()
    before = snapshot(g)
    assert g.query("nobody", 2, T0, T0).is_empty
    g.query("B", 6, T0, T0 + timedelta(hours=10))
    assert snapshot(g) == before


@pytest.mark.parametrize("hops", [0, 7, -1, True, 2.0])
def test_query_rejects_bad_hops(hops):
    with pytest.raises(GraphQueryError) as exc:
        _chain().query("A", hops, T0, T0)
    assert exc.value.parameter == "hops" and "hops" in str(exc.value)


def test_query_rejects_inverted_range():
    with pytest.raises(GraphQueryError) as exc:
        _chain().query("A", 2, T0 + timedelta(seconds=1), T0)
    assert exc.value.parameter == "start"


def test_invalid_window_rejected():
    with pytest.raises(ValueError):
        TemporalGraph(window_s=0)
