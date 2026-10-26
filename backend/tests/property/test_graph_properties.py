"""Property tests for helix.graph.TemporalGraph (design Properties 36-38)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.graph import Edge, GraphQueryError, TemporalGraph

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
WINDOW_S = 10 * 3_600.0
WINDOW = timedelta(seconds=WINDOW_S)
ACCOUNTS = list("ABCDEF")


@dataclass(frozen=True)
class Ev:
    tx_id: str
    ts: datetime
    src: str
    dst: str
    amount: Decimal
    currency: str


# Offsets in whole minutes over 30h (3x the window) so exact-boundary timestamps,
# ties, late and too-old events all occur; a small tx_id pool forces duplicates.
events = st.builds(
    Ev,
    tx_id=st.integers(0, 40).map(lambda i: f"tx{i}"),
    ts=st.integers(0, 30 * 60).map(lambda m: T0 + timedelta(minutes=m)),
    src=st.sampled_from(ACCOUNTS),
    dst=st.sampled_from(ACCOUNTS),
    amount=st.integers(1, 10_000_00).map(lambda c: Decimal(c) / 100),
    currency=st.sampled_from(["USD", "EUR"]),
)
sequences = st.lists(events, max_size=60)


class Model:
    """Reference: retained = accepted events with ts >= max accepted ts - window."""

    def __init__(self) -> None:
        self.latest: datetime | None = None
        self.retained: dict[str, Ev] = {}

    def add(self, e: Ev) -> bool:
        if self.latest is not None and e.ts < self.latest - WINDOW:
            return False
        if e.tx_id in self.retained:
            return False
        self.retained[e.tx_id] = e
        if self.latest is None or e.ts > self.latest:
            self.latest = e.ts
            cutoff = self.latest - WINDOW
            self.retained = {k: v for k, v in self.retained.items() if v.ts >= cutoff}
        return True


def build(seq: list[Ev]) -> tuple[TemporalGraph, Model]:
    g, m = TemporalGraph(window_s=WINDOW_S), Model()
    for e in seq:
        assert g.add(e) == m.add(e)
    return g, m


def snapshot(g: TemporalGraph):
    return (sorted(g.nodes()), g.edges(), g.latest)


def bfs_oracle(edges: list[Ev], account: str, hops: int, start: datetime, end: datetime):
    """Independent reference: undirected BFS over in-range edges; an edge is in the
    result when one endpoint lies at distance < hops."""
    in_range = [e for e in edges if start <= e.ts <= end]
    nodes_present = {n for e in edges for n in (e.src, e.dst)}
    if account not in nodes_present:
        return frozenset(), set()
    adj: dict[str, set[str]] = {}
    for e in in_range:
        adj.setdefault(e.src, set()).add(e.dst)
        adj.setdefault(e.dst, set()).add(e.src)
    dist = {account: 0}
    q = deque([account])
    while q:
        n = q.popleft()
        if dist[n] == hops:
            continue
        for nb in adj.get(n, ()):
            if nb not in dist:
                dist[nb] = dist[n] + 1
                q.append(nb)
    tx = {e.tx_id for e in in_range if min(dist.get(e.src, hops + 1), dist.get(e.dst, hops + 1)) < hops}
    return frozenset(dist), tx


# Feature: helix-fraud-immune-system, Property 36: Graph window invariant and node cleanup
@pytest.mark.property
@given(seq=sequences)
def test_graph_window_invariant_and_node_cleanup(seq):
    g, m = TemporalGraph(window_s=WINDOW_S), Model()
    for e in seq:
        assert g.add(e) == m.add(e)
        assert g.latest == m.latest
        cutoff = g.latest - WINDOW
        edges = g.edges()
        assert all(x.ts >= cutoff for x in edges)
        # Exactly one edge per accepted in-window event, carrying its attributes.
        assert {x.tx_id: x for x in edges} == {
            k: _edge_of(v) for k, v in m.retained.items()
        }
        assert len(edges) == len(m.retained) == g.num_edges
        # No degree-0 nodes: nodes are exactly the endpoints of retained edges.
        assert set(g.nodes()) == {n for x in edges for n in (x.src, x.dst)}
        assert all(g.g.degree(n) > 0 for n in g.nodes())


def _edge_of(e: Ev) -> Edge:
    return Edge(e.tx_id, e.src, e.dst, e.ts, e.amount, e.currency)


# Feature: helix-fraud-immune-system, Property 37: Queries are correct and do not mutate
@pytest.mark.property
@given(
    seq=sequences,
    account=st.sampled_from(ACCOUNTS + ["UNKNOWN"]),
    hops=st.integers(-1, 8),
    a=st.integers(-60, 31 * 60),
    b=st.integers(-60, 31 * 60),
)
def test_query_matches_bfs_oracle_and_does_not_mutate(seq, account, hops, a, b):
    g, m = build(seq)
    start, end = T0 + timedelta(minutes=a), T0 + timedelta(minutes=b)
    before = snapshot(g)
    if not 1 <= hops <= 6:
        with pytest.raises(GraphQueryError) as exc:
            g.query(account, hops, start, end)
        assert exc.value.parameter == "hops"
    elif start > end:
        with pytest.raises(GraphQueryError) as exc:
            g.query(account, hops, start, end)
        assert exc.value.parameter == "start"
    else:
        sub = g.query(account, hops, start, end)
        nodes, tx = bfs_oracle(list(m.retained.values()), account, hops, start, end)
        assert sub.nodes == nodes
        assert {e.tx_id for e in sub.edges} == tx and len(sub.edges) == len(tx)
        if not g.has_node(account):
            assert sub.is_empty
    assert snapshot(g) == before


# Feature: helix-fraud-immune-system, Property 38: Duplicate and too-old edges are ignored
@pytest.mark.property
@given(seq=st.lists(events, min_size=1, max_size=60), data=st.data())
def test_duplicate_and_too_old_edges_ignored(seq, data):
    g, _ = build(seq)
    before = snapshot(g)
    cutoff = g.latest - WINDOW
    kind = data.draw(st.sampled_from(["dup", "old"]) if g.edge_ids else st.just("old"))
    if kind == "dup":
        tx = data.draw(st.sampled_from(sorted(g.edge_ids)))
        ts = cutoff + timedelta(minutes=data.draw(st.integers(0, 2 * 10 * 60)))
    else:
        tx = "fresh-id"
        ts = cutoff - timedelta(minutes=data.draw(st.integers(1, 10 * 60)))
    probe = Ev(tx, ts, data.draw(st.sampled_from(ACCOUNTS + ["NEW"])), "NEW2", Decimal("1.00"), "USD")
    assert g.add(probe) is False
    assert snapshot(g) == before
