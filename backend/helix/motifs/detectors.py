"""Incremental, edge-anchored Motif_Detectors (design §10, Requirement 11).

Every detector evaluates only candidate instances that contain the new edge
(Req 11.1), using the edges currently retained in the Temporal_Graph. All
detectors search in both time directions from the new edge, so late
(out-of-order) edges are handled by the same code path as in-order ones:

- Round_Trip_Cycle: forward DFS from ``v`` over edges with increasing ts > t,
  joined at each reached account ``x`` with a backward DFS from ``u`` over edges
  with decreasing ts < t that ends at ``x``. For an in-order edge the forward
  side finds nothing and this is the plain backward DFS of the design.
- Mule_Chain: backward extension from ``u`` crossed with forward extension
  from ``v`` under the pairwise forwarding rule.
- Fan_Out / Fan_In / Structuring: the hub's edges within ±span of t are read
  from the graph and every maximal span-window containing t is checked. Reading
  from the graph (rather than a private per-node deque) keeps eviction and late
  edges consistent with the graph by construction; cost is O(hub degree) per edge.

Interval bounds are inclusive (≤ 72 h, ≤ 24 h, ≤ 48 h, ≤ 60 min). Amounts are
compared in Decimal, ignoring currency. The per-edge budget is checked with
``perf_counter`` inside every search loop; on timeout the instances found so
far are returned with ``truncated=True``.
"""

from __future__ import annotations

import heapq
import itertools
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Any, Protocol

from helix.fusion import Mass
from helix.graph import Edge, TemporalGraph
from helix.motifs.models import MotifInstance, MotifResult, MotifType, motif_id_for

ROUND_TRIP_MAX_ACCOUNTS = 6
ROUND_TRIP_SPAN = timedelta(hours=72)
FAN_MIN_COUNTERPARTIES = 8
FAN_SPAN = timedelta(hours=24)
STRUCTURING_MIN_COUNT = 3
STRUCTURING_SPAN = timedelta(hours=48)
STRUCTURING_BAND = Decimal("0.9")
MULE_MIN_RATIO = Decimal("0.8")
MULE_MAX_DELAY = timedelta(minutes=60)


class MotifDetector(Protocol):
    motif_type: MotifType

    def on_edge(self, e: Edge, g: TemporalGraph, deadline: float) -> MotifResult: ...


class _Timeout(Exception):
    pass


def _tick(deadline: float) -> None:
    if perf_counter() > deadline:
        raise _Timeout


def _edge(g: TemporalGraph, tx_id: str, src: str, dst: str) -> Edge:
    d = g.g.adj[src][dst][tx_id]
    return Edge(tx_id, src, dst, d["ts"], d["amount"], d["currency"])


# (tx_id, src, dst, ts, amount) — light tuples during search, materialised on emit.
_Hop = tuple[str, str, str, datetime, Decimal]
_Emit = Callable[[Sequence[_Hop]], None]


class _BaseDetector:
    motif_type: MotifType

    def __init__(self, mass: Mass) -> None:
        self.mass = mass
        self._emitted: set[frozenset[str]] = set()
        self._expiry: list[tuple[datetime, int, frozenset[str]]] = []
        self._seq = itertools.count()

    # -- dedup ---------------------------------------------------------------

    def _prune(self, cutoff: datetime | None) -> None:
        # An instance with an edge older than the cutoff can never be formed again:
        # that edge has been evicted and the graph rejects it as too old.
        if cutoff is None:
            return
        while self._expiry and self._expiry[0][0] < cutoff:
            self._emitted.discard(heapq.heappop(self._expiry)[2])

    def already_emitted(self, tx_ids: frozenset[str]) -> bool:
        return tx_ids in self._emitted

    # -- driver --------------------------------------------------------------

    def on_edge(self, e: Edge, g: TemporalGraph, deadline: float) -> MotifResult:
        self._prune(g.cutoff)
        found: list[tuple[frozenset[str], tuple[Edge, ...]]] = []

        def emit(hops: Sequence[_Hop]) -> None:
            key = frozenset(h[0] for h in hops)
            if key in self._emitted:
                return
            edges = tuple(_edge(g, h[0], h[1], h[2]) for h in hops)
            self._emitted.add(key)
            heapq.heappush(self._expiry, (min(x.ts for x in edges), next(self._seq), key))
            found.append((key, edges))

        truncated = False
        try:
            _tick(deadline)
            self._search(e, g, deadline, emit)
        except _Timeout:
            truncated = True
        instances = tuple(self._build(key, edges, truncated) for key, edges in found)
        return MotifResult(instances, truncated)

    def _search(self, e: Edge, g: TemporalGraph, deadline: float, emit: _Emit) -> None:
        raise NotImplementedError

    def _accounts_detail(self, edges: tuple[Edge, ...]) -> tuple[tuple[str, ...], dict[str, Any]]:
        raise NotImplementedError

    def _build(self, key: frozenset[str], edges: tuple[Edge, ...], truncated: bool) -> MotifInstance:
        accounts, detail = self._accounts_detail(edges)
        ts = [x.ts for x in edges]
        return MotifInstance(
            motif_id=motif_id_for(self.motif_type, key),
            motif_type=self.motif_type,
            tx_ids=key,
            accounts=accounts,
            ts_start=min(ts),
            ts_end=max(ts),
            mass=self.mass,
            truncated=truncated,
            edges=edges,
            detail=detail,
        )


# ---------------------------------------------------------------------------- cycles


class RoundTripDetector(_BaseDetector):
    motif_type = MotifType.ROUND_TRIP

    def __init__(
        self, mass: Mass, max_accounts: int = ROUND_TRIP_MAX_ACCOUNTS, span: timedelta = ROUND_TRIP_SPAN
    ) -> None:
        super().__init__(mass)
        if max_accounts < 2:
            raise ValueError("max_accounts must be at least 2")
        self.max_edges = max_accounts  # a cycle of k accounts has k edges
        self.span = span

    def _search(self, e: Edge, g: TemporalGraph, deadline: float, emit: _Emit) -> None:
        u, v, t = e.src, e.dst, e.ts
        if u == v:
            return  # self-loop: a cycle needs at least 2 accounts
        max_edges, span = self.max_edges, self.span
        me: _Hop = (e.tx_id, u, v, t, e.amount)
        used = {u, v}
        fwd: list[_Hop] = []  # v -> ... -> x, ts increasing, all > t
        bwd: list[_Hop] = []  # collected walking back from u, ts decreasing, all < t

        def close(target: str, latest: datetime) -> None:
            """Backward DFS from u to ``target`` with strictly decreasing ts < t."""
            remaining = max_edges - 1 - len(fwd)
            if remaining < 1:
                return
            lo = latest - span

            def back(y: str, cur_ts: datetime) -> None:
                for tx, w, ts, amt in g.iter_in_edges(y):
                    _tick(deadline)
                    if ts >= cur_ts or ts < lo:
                        continue
                    if w == target:
                        bwd.append((tx, w, y, ts, amt))
                        emit([*reversed(bwd), me, *fwd])
                        bwd.pop()
                    elif w not in used and len(bwd) + 2 <= remaining:
                        used.add(w)
                        bwd.append((tx, w, y, ts, amt))
                        back(w, ts)
                        bwd.pop()
                        used.discard(w)

            back(u, t)

        def forward(x: str, last_ts: datetime) -> None:
            close(x, last_ts)
            if len(fwd) + 2 > max_edges:  # e + fwd + at least one more edge
                return
            for tx, y, ts, amt in g.iter_out_edges(x):
                _tick(deadline)
                if ts <= last_ts or ts - t > span:
                    continue
                if y == u:  # e is the earliest edge of this cycle
                    fwd.append((tx, x, y, ts, amt))
                    emit([me, *fwd])
                    fwd.pop()
                elif y not in used and len(fwd) + 3 <= max_edges:
                    used.add(y)
                    fwd.append((tx, x, y, ts, amt))
                    forward(y, ts)
                    fwd.pop()
                    used.discard(y)

        forward(v, t)

    def _accounts_detail(self, edges: tuple[Edge, ...]) -> tuple[tuple[str, ...], dict[str, Any]]:
        # ``edges`` is in traversal order, which equals strictly increasing ts order.
        return tuple(x.src for x in edges), {
            "tx_ids_in_order": tuple(x.tx_id for x in edges),
            "duration_s": (edges[-1].ts - edges[0].ts).total_seconds(),
        }


# ---------------------------------------------------------------------------- mule chains


class MuleChainDetector(_BaseDetector):
    motif_type = MotifType.MULE_CHAIN

    def __init__(
        self, mass: Mass, min_ratio: Decimal = MULE_MIN_RATIO, max_delay: timedelta = MULE_MAX_DELAY
    ) -> None:
        super().__init__(mass)
        self.min_ratio = Decimal(min_ratio)
        self.max_delay = max_delay

    def _search(self, e: Edge, g: TemporalGraph, deadline: float, emit: _Emit) -> None:
        u, v, t, a = e.src, e.dst, e.ts, e.amount
        if u == v:
            return
        ratio, delay = self.min_ratio, self.max_delay
        me: _Hop = (e.tx_id, u, v, t, a)
        used = {u, v}
        bwd: list[_Hop] = []  # walking back from u
        fwd: list[_Hop] = []  # walking forward from v

        def forward(x: str, in_ts: datetime, in_amt: Decimal) -> None:
            for tx, y, ts, amt in g.iter_out_edges(x):
                _tick(deadline)
                if ts < in_ts or ts - in_ts > delay or amt < ratio * in_amt or y in used:
                    continue
                used.add(y)
                fwd.append((tx, x, y, ts, amt))
                emit([*reversed(bwd), me, *fwd])
                forward(y, ts, amt)
                fwd.pop()
                used.discard(y)

        def back(y: str, out_ts: datetime, out_amt: Decimal) -> None:
            for tx, w, ts, amt in g.iter_in_edges(y):
                _tick(deadline)
                if ts > out_ts or out_ts - ts > delay or out_amt < ratio * amt or w in used:
                    continue
                used.add(w)
                bwd.append((tx, w, y, ts, amt))
                emit([*reversed(bwd), me])
                forward(v, t, a)
                back(w, ts, amt)
                bwd.pop()
                used.discard(w)

        forward(v, t, a)  # chains in which e is the first hop
        back(u, t, a)

    def _accounts_detail(self, edges: tuple[Edge, ...]) -> tuple[tuple[str, ...], dict[str, Any]]:
        pairs = list(zip(edges, edges[1:]))
        return (*(x.src for x in edges), edges[-1].dst), {
            "forwarding_ratios": tuple(float(b.amount / a.amount) if a.amount else float("inf") for a, b in pairs),
            "hop_delays_s": tuple((b.ts - a.ts).total_seconds() for a, b in pairs),
        }


# ---------------------------------------------------------------------------- windowed counts


def _windows(
    entries: list[tuple[datetime, str, str, Decimal]], t: datetime, span: timedelta, deadline: float
) -> list[list[tuple[datetime, str, str, Decimal]]]:
    """Maximal windows ``[s, s + span]`` with ``s`` an entry ts in ``[t - span, t]``.

    ``entries`` must be sorted. A window is dropped when it equals a subset of the
    previous one (same end index), so only maximal windows containing t remain.
    """
    times = [x[0] for x in entries]
    out = []
    prev_end = -1
    i = bisect_left(times, t - span)
    n = len(times)
    while i < n and times[i] <= t:
        _tick(deadline)
        start = times[i]
        end = bisect_right(times, start + span)
        if end > prev_end:
            out.append(entries[i:end])
            prev_end = end
        i = bisect_right(times, start)
    return out


class _FanDetector(_BaseDetector):
    outgoing: bool

    def __init__(self, mass: Mass, min_counterparties: int = FAN_MIN_COUNTERPARTIES, span: timedelta = FAN_SPAN):
        super().__init__(mass)
        self.k = min_counterparties
        self.span = span

    def _search(self, e: Edge, g: TemporalGraph, deadline: float, emit: _Emit) -> None:
        if e.src == e.dst:
            return  # a self-loop has no counterparty
        hub = e.src if self.outgoing else e.dst
        t, span = e.ts, self.span
        it = g.iter_out_edges(hub) if self.outgoing else g.iter_in_edges(hub)
        entries = []
        for tx, cp, ts, amt in it:
            _tick(deadline)
            if cp != hub and abs(ts - t) <= span:
                entries.append((ts, tx, cp, amt))
        if len({x[2] for x in entries}) < self.k:
            return
        entries.sort()
        for w in _windows(entries, t, span, deadline):
            if len({x[2] for x in w}) >= self.k:
                if self.outgoing:
                    emit([(tx, hub, cp, ts, amt) for ts, tx, cp, amt in w])
                else:
                    emit([(tx, cp, hub, ts, amt) for ts, tx, cp, amt in w])

    def _accounts_detail(self, edges: tuple[Edge, ...]) -> tuple[tuple[str, ...], dict[str, Any]]:
        hub = edges[0].src if self.outgoing else edges[0].dst
        cps = tuple(sorted({x.dst if self.outgoing else x.src for x in edges}))
        return (hub, *cps), {
            "hub": hub,
            "counterparties": cps,
            "window_start": edges[0].ts.isoformat(),
            "window_end": edges[-1].ts.isoformat(),
        }


class FanOutDetector(_FanDetector):
    motif_type = MotifType.FAN_OUT
    outgoing = True


class FanInDetector(_FanDetector):
    motif_type = MotifType.FAN_IN
    outgoing = False


class StructuringDetector(_BaseDetector):
    motif_type = MotifType.STRUCTURING

    def __init__(
        self,
        mass: Mass,
        reporting_threshold: Decimal | float | str = Decimal("10000"),
        min_count: int = STRUCTURING_MIN_COUNT,
        span: timedelta = STRUCTURING_SPAN,
    ) -> None:
        super().__init__(mass)
        rt = Decimal(str(reporting_threshold))
        if not rt > 0:
            raise ValueError("reporting_threshold must be positive")
        self.rt = rt
        self.lo = STRUCTURING_BAND * rt
        self.k = min_count
        self.span = span

    def in_band(self, amount: Decimal) -> bool:
        return self.lo <= amount < self.rt

    def _search(self, e: Edge, g: TemporalGraph, deadline: float, emit: _Emit) -> None:
        if not self.in_band(e.amount):
            return  # no candidate containing e can qualify
        src, t, span = e.src, e.ts, self.span
        entries = []
        for tx, dst, ts, amt in g.iter_out_edges(src):
            _tick(deadline)
            if abs(ts - t) <= span and self.in_band(amt):
                entries.append((ts, tx, dst, amt))
        if len(entries) < self.k:
            return
        entries.sort()
        for w in _windows(entries, t, span, deadline):
            if len(w) >= self.k:
                emit([(tx, src, dst, ts, amt) for ts, tx, dst, amt in w])

    def _accounts_detail(self, edges: tuple[Edge, ...]) -> tuple[tuple[str, ...], dict[str, Any]]:
        return (edges[0].src,), {
            "reporting_threshold": str(self.rt),
            "amounts": tuple(str(x.amount) for x in edges),
            "tx_ids_in_order": tuple(x.tx_id for x in edges),
        }
