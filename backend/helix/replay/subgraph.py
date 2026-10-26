"""Related_Subgraph of a Case (Req 17.1-17.3, 17.6).

A multi-source BFS from the Case accounts over the Temporal_Graph, both directions,
using only edges with ``start <= ts <= end``. The traversal rule matches
``TemporalGraph.query``: an edge is included when one of its endpoints is at hop
distance ``< hops``, so without truncation the result equals the union of
``graph.query(account, hops, start, end)`` over the Case accounts.

Truncation keeps every Case account plus the non-case nodes nearest by hop distance
(ties by account id) up to ``max_nodes``, keeps only edges between kept nodes and
reports the number of omitted nodes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from helix.graph import TemporalGraph
from helix.graph.temporal_graph import Edge

from .errors import ReplayParameterError, UnknownAccountError

HOPS_MIN, HOPS_MAX = 1, 4
MAX_NODES_MIN, MAX_NODES_MAX = 50, 500


@dataclass(frozen=True, slots=True)
class SubgraphNode:
    account: str
    is_case_account: bool
    hop: int  # hop distance from the nearest Case account (0 for Case accounts)


@dataclass(frozen=True, slots=True)
class RelatedSubgraph:
    """Nodes sorted by ``(hop, account)``; edges by ``(ts, tx_id)``."""

    nodes: tuple[SubgraphNode, ...]
    edges: tuple[Edge, ...]
    case_accounts: frozenset[str]
    start: datetime | None  # edge time range used (None = unbounded)
    end: datetime | None
    hops: int
    max_nodes: int
    truncated: bool
    omitted: int  # nodes left out by truncation

    def node(self, account: str) -> SubgraphNode | None:
        for n in self.nodes:
            if n.account == account:
                return n
        return None

    @property
    def accounts(self) -> frozenset[str]:
        return frozenset(n.account for n in self.nodes)


def check_int_range(name: str, value: object, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ReplayParameterError(name, value, lo, hi)
    return value


def _incident(graph: TemporalGraph, n: str, start: datetime | None, end: datetime | None) -> list[Edge]:
    return graph.out_edges(n, start, end) + graph.in_edges(n, start, end)


def related_subgraph(
    case_accounts: Iterable[str],
    graph: TemporalGraph,
    hops: int = 2,
    max_nodes: int = 500,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> RelatedSubgraph:
    """Related_Subgraph of ``case_accounts`` within ``hops``. ``start``/``end`` default to
    the graph's retained window ``[latest - window, latest]``."""
    hops = check_int_range("replay.hops", hops, HOPS_MIN, HOPS_MAX)
    max_nodes = check_int_range("replay.max_nodes", max_nodes, MAX_NODES_MIN, MAX_NODES_MAX)
    if start is None and end is None and graph.latest is not None:
        start, end = graph.cutoff, graph.latest
    cases = frozenset(case_accounts)

    dist: dict[str, int] = {a: 0 for a in cases}
    edges: dict[str, Edge] = {}
    queue: deque[str] = deque(sorted(cases))
    while queue:
        n = queue.popleft()
        d = dist[n]
        if d >= hops:
            continue
        for e in _incident(graph, n, start, end):
            edges.setdefault(e.tx_id, e)
            other = e.dst if e.src == n else e.src
            if other not in dist:
                dist[other] = d + 1
                queue.append(other)

    others = sorted((a for a in dist if a not in cases), key=lambda a: (dist[a], a))
    room = max(0, max_nodes - len(cases))
    kept = set(cases) | set(others[:room])
    omitted = len(dist) - len(kept)
    nodes = tuple(
        sorted((SubgraphNode(a, a in cases, dist[a]) for a in kept), key=lambda x: (x.hop, x.account))
    )
    kept_edges = tuple(
        sorted(
            (e for e in edges.values() if e.src in kept and e.dst in kept),
            key=lambda e: (e.ts, e.tx_id),
        )
    )
    return RelatedSubgraph(nodes, kept_edges, cases, start, end, hops, max_nodes, omitted > 0, omitted)


@dataclass(frozen=True, slots=True)
class Neighbourhood:
    """Direct neighbours of one account and the connecting edges (Req 17.2)."""

    account: str
    nodes: tuple[SubgraphNode, ...]  # the account first, then neighbours by account id
    edges: tuple[Edge, ...]


def expand(sub: RelatedSubgraph, graph: TemporalGraph, account: str) -> Neighbourhood:
    """Neighbours of ``account`` within the subgraph's time range. Raises
    ``UnknownAccountError`` if ``account`` is not a node of ``sub``."""
    me = sub.node(account)
    if me is None:
        raise UnknownAccountError(account)
    es = {e.tx_id: e for e in _incident(graph, account, sub.start, sub.end)}
    known = {n.account: n for n in sub.nodes}
    nbrs: dict[str, SubgraphNode] = {}
    for e in es.values():
        other = e.dst if e.src == account else e.src
        if other == account or other in nbrs:
            continue
        hop = known[other].hop if other in known else me.hop + 1
        nbrs[other] = SubgraphNode(other, other in sub.case_accounts, hop)
    edges = tuple(sorted(es.values(), key=lambda e: (e.ts, e.tx_id)))
    return Neighbourhood(account, (me, *(nbrs[a] for a in sorted(nbrs))), edges)


__all__ = [
    "HOPS_MAX",
    "HOPS_MIN",
    "MAX_NODES_MAX",
    "MAX_NODES_MIN",
    "Neighbourhood",
    "RelatedSubgraph",
    "SubgraphNode",
    "check_int_range",
    "expand",
    "related_subgraph",
]
