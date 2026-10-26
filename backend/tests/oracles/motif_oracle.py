"""Brute-force reference Motif detectors (design §10; test-only).

Written straight from the glossary, independent of the incremental detectors:
full enumeration over a small edge set, no anchoring on a new edge, no budget.
All interval bounds are inclusive; amounts are compared in Decimal.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal

from helix.graph import Edge

H = timedelta(hours=1)
CYCLE_SPAN, CYCLE_MAX = 72 * H, 6
FAN_SPAN, FAN_K = 24 * H, 8
STRUCT_SPAN, STRUCT_K = 48 * H, 3
MULE_DELAY, MULE_RATIO = timedelta(minutes=60), Decimal("0.8")


# ---------------------------------------------------------------- definition predicates


def is_cycle(edges: list[Edge]) -> bool:
    es = sorted(edges, key=lambda x: x.ts)
    k = len(es)
    if not 2 <= k <= CYCLE_MAX:
        return False
    if any(a.ts >= b.ts for a, b in zip(es, es[1:])):
        return False
    if any(a.dst != b.src for a, b in zip(es, es[1:])) or es[-1].dst != es[0].src:
        return False
    if len({x.src for x in es}) != k:
        return False
    return es[-1].ts - es[0].ts <= CYCLE_SPAN


def mule_ordered(edges: list[Edge]) -> list[Edge] | None:
    """Return the edges in flow order if they form one simple path, else None."""
    by_src = {x.src: x for x in edges}
    if len(by_src) != len(edges):
        return None
    dsts = {x.dst for x in edges}
    heads = [x for x in edges if x.src not in dsts]
    if len(heads) != 1:
        return None
    path = [heads[0]]
    while path[-1].dst in by_src and len(path) <= len(edges):
        path.append(by_src[path[-1].dst])
    return path if len(path) == len(edges) else None


def is_mule_chain(edges: list[Edge]) -> bool:
    if len(edges) < 2:
        return False
    path = mule_ordered(edges)
    if path is None:
        return False
    accounts = [x.src for x in path] + [path[-1].dst]
    if len(set(accounts)) != len(accounts):
        return False
    for a, b in zip(path, path[1:]):
        if not (timedelta(0) <= b.ts - a.ts <= MULE_DELAY):
            return False
        if b.amount < MULE_RATIO * a.amount:
            return False
    return True


def is_fan(edges: list[Edge], outgoing: bool) -> bool:
    hubs = {x.src if outgoing else x.dst for x in edges}
    if len(hubs) != 1:
        return False
    hub = next(iter(hubs))
    cps = {x.dst if outgoing else x.src for x in edges}
    if hub in cps:
        return False
    ts = [x.ts for x in edges]
    return len(cps) >= FAN_K and max(ts) - min(ts) <= FAN_SPAN


def in_band(amount: Decimal, rt: Decimal) -> bool:
    return Decimal("0.9") * rt <= amount < rt


def is_structuring(edges: list[Edge], rt: Decimal) -> bool:
    if len({x.src for x in edges}) != 1 or len(edges) < STRUCT_K:
        return False
    ts = [x.ts for x in edges]
    return all(in_band(x.amount, rt) for x in edges) and max(ts) - min(ts) <= STRUCT_SPAN


def satisfies(motif_type: str, edges: list[Edge], rt: Decimal) -> bool:
    return {
        "ROUND_TRIP_CYCLE": lambda: is_cycle(edges),
        "MULE_CHAIN": lambda: is_mule_chain(edges),
        "FAN_OUT": lambda: is_fan(edges, True),
        "FAN_IN": lambda: is_fan(edges, False),
        "STRUCTURING": lambda: is_structuring(edges, rt),
    }[motif_type]()


# ---------------------------------------------------------------- full enumeration


def _out_index(edges: Iterable[Edge]) -> dict[str, list[Edge]]:
    out: dict[str, list[Edge]] = defaultdict(list)
    for x in edges:
        out[x.src].append(x)
    return out


def all_cycles(edges: list[Edge]) -> set[frozenset[str]]:
    """Every Round_Trip_Cycle, found once from its earliest edge."""
    out_idx = _out_index(edges)
    found: set[frozenset[str]] = set()

    def dfs(path: list[Edge], seen: set[str]) -> None:
        last, first = path[-1], path[0]
        for nx in out_idx[last.dst]:
            if nx.ts <= last.ts or nx.ts - first.ts > CYCLE_SPAN:
                continue
            if nx.dst == first.src and len(path) + 1 >= 2:
                if len(path) + 1 <= CYCLE_MAX:
                    found.add(frozenset(x.tx_id for x in path + [nx]))
            elif nx.dst not in seen and len(path) + 1 < CYCLE_MAX:
                dfs(path + [nx], seen | {nx.dst})

    for e in edges:
        if e.src != e.dst:
            dfs([e], {e.src, e.dst})
    return found


def all_mule_chains(edges: list[Edge]) -> set[frozenset[str]]:
    """Every Mule_Chain (all simple forwarding paths of >= 2 hops)."""
    out_idx = _out_index(edges)
    found: set[frozenset[str]] = set()

    def dfs(path: list[Edge], seen: set[str]) -> None:
        last = path[-1]
        for nx in out_idx[last.dst]:
            if nx.dst in seen:
                continue
            if not (timedelta(0) <= nx.ts - last.ts <= MULE_DELAY) or nx.amount < MULE_RATIO * last.amount:
                continue
            p = path + [nx]
            found.add(frozenset(x.tx_id for x in p))
            dfs(p, seen | {nx.dst})

    for e in edges:
        if e.src != e.dst:
            dfs([e], {e.src, e.dst})
    return found


def fan_windows(edges: list[Edge], outgoing: bool) -> list[frozenset[str]]:
    """For each hub and each of its edge timestamps s, the hub's edges in [s, s + 24h]
    when they reach 8 distinct counterparties. Any qualifying edge set is a subset of one."""
    groups: dict[str, list[Edge]] = defaultdict(list)
    for x in edges:
        if x.src != x.dst:
            groups[x.src if outgoing else x.dst].append(x)
    out = []
    for es in groups.values():
        for s in {x.ts for x in es}:
            w = [x for x in es if s <= x.ts <= s + FAN_SPAN]
            if len({x.dst if outgoing else x.src for x in w}) >= FAN_K:
                out.append(frozenset(x.tx_id for x in w))
    return out


def structuring_windows(edges: list[Edge], rt: Decimal) -> list[frozenset[str]]:
    groups: dict[str, list[Edge]] = defaultdict(list)
    for x in edges:
        if in_band(x.amount, rt):
            groups[x.src].append(x)
    out = []
    for es in groups.values():
        for s in {x.ts for x in es}:
            w = [x for x in es if s <= x.ts <= s + STRUCT_SPAN]
            if len(w) >= STRUCT_K:
                out.append(frozenset(x.tx_id for x in w))
    return out
