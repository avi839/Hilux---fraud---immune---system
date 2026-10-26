"""Streaming temporal transaction graph (design §9, Requirement 10).

A NetworkX ``MultiDiGraph`` whose nodes are accounts and whose edges are
timestamped money flows keyed by ``tx_id``. Edges older than
``latest - window`` are evicted through a min-heap (late events arrive out of
ts order, so a deque would not stay sorted). Nodes left with degree 0 are
removed immediately after each eviction.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterator, Protocol

import networkx as nx

MIN_HOPS = 1
MAX_HOPS = 6
DEFAULT_WINDOW_S = 7 * 86_400.0


class GraphEvent(Protocol):
    """Minimal structural view of a TransactionEvent needed by the graph."""

    @property
    def tx_id(self) -> str: ...

    @property
    def ts(self) -> datetime: ...

    @property
    def src(self) -> str: ...

    @property
    def dst(self) -> str: ...

    @property
    def amount(self) -> Decimal: ...

    @property
    def currency(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Edge:
    tx_id: str
    src: str
    dst: str
    ts: datetime
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class Subgraph:
    """Immutable copy of a query result. Edges are ordered by ``(ts, tx_id)``."""

    nodes: frozenset[str]
    edges: tuple[Edge, ...]

    @classmethod
    def empty(cls) -> "Subgraph":
        return cls(frozenset(), ())

    @property
    def is_empty(self) -> bool:
        return not self.nodes and not self.edges


class GraphQueryError(ValueError):
    """Invalid query parameter (Req 10.11). ``parameter`` names the offending argument."""

    def __init__(self, parameter: str, message: str) -> None:
        super().__init__(f"{parameter}: {message}")
        self.parameter = parameter


def _sort_key(e: Edge) -> tuple[datetime, str]:
    return (e.ts, e.tx_id)


class TemporalGraph:
    def __init__(self, window_s: float = DEFAULT_WINDOW_S) -> None:
        if not window_s > 0:
            raise ValueError("window_s must be positive")
        self.window: timedelta = timedelta(seconds=window_s)
        self.g: nx.MultiDiGraph = nx.MultiDiGraph()
        self.edge_ids: set[str] = set()
        self._heap: list[tuple[datetime, str, str, str]] = []
        self.latest: datetime | None = None

    @classmethod
    def from_config(cls, graph_cfg) -> "TemporalGraph":
        """Build from a ``GraphConfig`` (uses ``window_s``)."""
        return cls(window_s=graph_cfg.window_s)

    # ------------------------------------------------------------------ mutation

    @property
    def cutoff(self) -> datetime | None:
        """Oldest retained timestamp (``latest - window``), or None when empty-history."""
        return None if self.latest is None else self.latest - self.window

    def add(self, ev: GraphEvent) -> bool:
        """Add one edge for ``ev``. Returns False (no change) if too old or duplicate."""
        ts = ev.ts
        cutoff = self.cutoff
        if cutoff is not None and ts < cutoff:
            return False  # Req 10.9
        if ev.tx_id in self.edge_ids:
            return False  # Req 10.10
        self.g.add_edge(ev.src, ev.dst, key=ev.tx_id, ts=ts, amount=ev.amount, currency=ev.currency)
        self.edge_ids.add(ev.tx_id)
        heapq.heappush(self._heap, (ts, ev.tx_id, ev.src, ev.dst))
        if self.latest is None or ts > self.latest:
            self.latest = ts
            self._evict(ts - self.window)
        return True

    def _evict(self, cutoff: datetime) -> None:
        heap, g = self._heap, self.g
        while heap and heap[0][0] < cutoff:  # boundary edge (== cutoff) is kept
            _, tx_id, u, v = heapq.heappop(heap)
            g.remove_edge(u, v, key=tx_id)
            self.edge_ids.discard(tx_id)
            for n in (u, v):
                if n in g and g.degree(n) == 0:
                    g.remove_node(n)

    # ------------------------------------------------------------------ read helpers

    def __contains__(self, tx_id: object) -> bool:
        return tx_id in self.edge_ids

    @property
    def num_nodes(self) -> int:
        return self.g.number_of_nodes()

    @property
    def num_edges(self) -> int:
        return len(self.edge_ids)

    def has_node(self, account: str) -> bool:
        return account in self.g

    def nodes(self) -> list[str]:
        return list(self.g.nodes)

    def edges(self) -> list[Edge]:
        """All retained edges ordered by ``(ts, tx_id)``."""
        out = [Edge(k, u, v, d["ts"], d["amount"], d["currency"]) for u, v, k, d in self.g.edges(keys=True, data=True)]
        out.sort(key=_sort_key)
        return out

    def out_edges(self, account: str, start: datetime | None = None, end: datetime | None = None) -> list[Edge]:
        """Edges leaving ``account`` with ``start <= ts <= end`` (bounds optional), ordered by ``(ts, tx_id)``."""
        if account not in self.g:
            return []
        return self._collect(self.g.out_edges(account, keys=True, data=True), start, end)

    def in_edges(self, account: str, start: datetime | None = None, end: datetime | None = None) -> list[Edge]:
        """Edges entering ``account`` with ``start <= ts <= end`` (bounds optional), ordered by ``(ts, tx_id)``."""
        if account not in self.g:
            return []
        return self._collect(self.g.in_edges(account, keys=True, data=True), start, end)

    @staticmethod
    def _collect(it, start: datetime | None, end: datetime | None) -> list[Edge]:
        out = [
            Edge(k, u, v, d["ts"], d["amount"], d["currency"])
            for u, v, k, d in it
            if (start is None or d["ts"] >= start) and (end is None or d["ts"] <= end)
        ]
        out.sort(key=_sort_key)
        return out

    def iter_out_edges(self, account: str) -> Iterator[tuple[str, str, datetime, Decimal]]:
        """Unsorted, allocation-light ``(tx_id, dst, ts, amount)`` tuples for hot DFS loops."""
        if account in self.g:
            for _, v, k, d in self.g.out_edges(account, keys=True, data=True):
                yield k, v, d["ts"], d["amount"]

    def iter_in_edges(self, account: str) -> Iterator[tuple[str, str, datetime, Decimal]]:
        """Unsorted, allocation-light ``(tx_id, src, ts, amount)`` tuples for hot DFS loops."""
        if account in self.g:
            for u, _, k, d in self.g.in_edges(account, keys=True, data=True):
                yield k, u, d["ts"], d["amount"]

    # ------------------------------------------------------------------ query

    def query(self, account: str, hops: int, start: datetime, end: datetime) -> Subgraph:
        """Nodes and edges reachable from ``account`` within ``hops``, both directions,
        using only edges with ``start <= ts <= end``. Never mutates the graph.

        An edge is included when it is traversed as hop ``i <= hops``, i.e. one of its
        endpoints is at BFS distance ``< hops``. The start account is included whenever
        it has a node; an unknown account yields an empty subgraph (Req 10.8).
        """
        if isinstance(hops, bool) or not isinstance(hops, int) or not MIN_HOPS <= hops <= MAX_HOPS:
            raise GraphQueryError("hops", f"must be an integer from {MIN_HOPS} to {MAX_HOPS}, got {hops!r}")
        if start > end:
            raise GraphQueryError("start", f"range start {start.isoformat()} is later than end {end.isoformat()}")
        g = self.g
        if account not in g:
            return Subgraph.empty()

        dist: dict[str, int] = {account: 0}
        edges: dict[str, Edge] = {}
        queue: deque[str] = deque([account])
        while queue:
            n = queue.popleft()
            d_n = dist[n]
            if d_n >= hops:
                continue
            for u, v, k, data in _both(g, n):
                ts = data["ts"]
                if ts < start or ts > end:
                    continue
                if k not in edges:
                    edges[k] = Edge(k, u, v, ts, data["amount"], data["currency"])
                other = v if u == n else u
                if other not in dist:
                    dist[other] = d_n + 1
                    queue.append(other)
        return Subgraph(frozenset(dist), tuple(sorted(edges.values(), key=_sort_key)))


def _both(g: nx.MultiDiGraph, n: str):
    yield from g.out_edges(n, keys=True, data=True)
    yield from g.in_edges(n, keys=True, data=True)
