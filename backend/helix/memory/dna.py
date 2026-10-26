"""Behavioural_DNA: per-account decayed statistics (design §7, Data Models).

This module depends only on the structural :class:`EventLike` protocol, so it does not
import ``helix.codec``. The codec's ``TransactionEvent`` satisfies the protocol.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

import numpy as np
from sortedcontainers import SortedList

RECENT_WINDOW = timedelta(hours=24)  # recent timestamps kept for Velocity
DAY_S = 86_400.0


@runtime_checkable
class EventLike(Protocol):
    """Minimal structural view of a TransactionEvent used by the Profile_Store."""

    tx_id: str
    ts: datetime
    src: str
    dst: str
    amount: Decimal | float
    lat: float | None
    lon: float | None
    channel: str | None
    device_id: str | None


@dataclass(slots=True)
class DecayedStats:
    """Decay-weighted mean / variance accumulator (weighted West update, §7).

    Values are stored relative to ``shift`` (the first sample) so that a large mean with a
    small spread does not lose precision: ``mean = shift + off`` and ``var = m2 / w``.
    """

    w: float = 0.0
    off: float = 0.0
    m2: float = 0.0
    shift: float = 0.0

    @property
    def mean(self) -> float:
        return self.shift + self.off if self.w > 0.0 else 0.0

    @property
    def var(self) -> float:
        return self.m2 / self.w if self.w > 0.0 else 0.0

    def update(self, x: float, d: float) -> None:
        """Decay prior weight by ``d`` then add ``x`` with weight 1.

        Equal in exact arithmetic to ``W = dW + 1, δ = x - mean, mean += δ/W,
        M2 = dM2 + δ(x - mean)``; ``δ(x - mean_new) = δ²·W_old/W_new`` is used directly
        because it has no cancellation when the new sample dominates the weight.
        """
        if self.w == 0.0:
            self.w, self.off, self.m2, self.shift = 1.0, 0.0, 0.0, x
            return
        w_old = self.w * d
        w_new = w_old + 1.0
        delta = (x - self.shift) - self.off
        self.off += delta / w_new
        self.m2 = self.m2 * d + delta * delta * (w_old / w_new)
        self.w = w_new

    def copy(self) -> DecayedStats:
        return DecayedStats(self.w, self.off, self.m2, self.shift)


@dataclass(slots=True)
class BehaviouralDNA:
    account: str
    count: int = 0  # applied events
    t_last: datetime | None = None  # running max ts (Δt = 0 rule)
    amount: DecayedStats = field(default_factory=DecayedStats)
    iat: DecayedStats = field(default_factory=DecayedStats)  # inter-arrival seconds
    hour_hist: np.ndarray = field(default_factory=lambda: np.zeros(24))
    weekday_hist: np.ndarray = field(default_factory=lambda: np.zeros(7))
    # Decayed channel counts, stored as of ``channel_t`` (decayed lazily on the next
    # channel-bearing event so that a missing channel leaves them untouched, Req 8.3).
    channel_counts: dict[str, float] = field(default_factory=dict)
    channel_t: datetime | None = None
    last_location: tuple[float, float, datetime] | None = None  # lat, lon, ts
    counterparties: set[str] = field(default_factory=set)
    recent_ts: SortedList = field(default_factory=SortedList)  # last 24 h, Velocity

    def copy(self) -> BehaviouralDNA:
        return BehaviouralDNA(
            account=self.account,
            count=self.count,
            t_last=self.t_last,
            amount=self.amount.copy(),
            iat=self.iat.copy(),
            hour_hist=self.hour_hist.copy(),
            weekday_hist=self.weekday_hist.copy(),
            channel_counts=dict(self.channel_counts),
            channel_t=self.channel_t,
            last_location=self.last_location,
            counterparties=set(self.counterparties),
            recent_ts=SortedList(self.recent_ts),
        )

    def reset_regime_stats(self) -> None:
        """Clear the stats rebuilt by a rebase; keeps count, counterparties, t_last, recent_ts."""
        self.amount = DecayedStats()
        self.iat = DecayedStats()
        self.hour_hist = np.zeros(24)
        self.weekday_hist = np.zeros(7)
        self.channel_counts = {}
        self.channel_t = None
        self.last_location = None


def decay_factor(dt_s: float, half_life_s: float) -> float:
    """``0.5^(Δt/h)`` with Δt clamped at 0."""
    return 1.0 if dt_s <= 0.0 else 0.5 ** (dt_s / half_life_s)


def apply_event(dna: BehaviouralDNA, ev: EventLike, half_life_s: float) -> None:
    """Apply one event to ``dna`` in place. O(1) amortised (O(log n) recent-ts insert)."""
    ts = ev.ts
    if dna.t_last is None:
        dt_s, d = 0.0, 1.0
        dna.t_last = ts
        iat: float | None = None
    else:
        dt_s = max(0.0, (ts - dna.t_last).total_seconds())  # late / earlier events: Δt = 0
        d = decay_factor(dt_s, half_life_s)
        iat = dt_s
        if ts > dna.t_last:
            dna.t_last = ts

    dna.count += 1
    dna.amount.update(float(ev.amount), d)
    if iat is not None:
        dna.iat.update(iat, d)

    utc = ts.astimezone(timezone.utc)
    if d != 1.0:
        dna.hour_hist *= d
        dna.weekday_hist *= d
    dna.hour_hist[utc.hour] += 1.0
    dna.weekday_hist[utc.weekday()] += 1.0

    channel = ev.channel
    if channel is not None:
        if dna.channel_t is not None and ts > dna.channel_t:
            dc = decay_factor((ts - dna.channel_t).total_seconds(), half_life_s)
            for k in dna.channel_counts:
                dna.channel_counts[k] *= dc
            dna.channel_t = ts
        elif dna.channel_t is None:
            dna.channel_t = ts
        dna.channel_counts[channel] = dna.channel_counts.get(channel, 0.0) + 1.0

    lat, lon = ev.lat, ev.lon
    if lat is not None and lon is not None:
        if dna.last_location is None or ts >= dna.last_location[2]:
            dna.last_location = (float(lat), float(lon), ts)

    dna.counterparties.add(ev.dst)

    cutoff = dna.t_last - RECENT_WINDOW
    recent = dna.recent_ts
    if ts >= cutoff:
        recent.add(ts)
    # Batched pruning: only once the oldest entry is 48 h old (views filter to 24 h).
    if recent and recent[0] < cutoff - RECENT_WINDOW:
        del recent[: recent.bisect_left(cutoff)]


def recent_window(dna: BehaviouralDNA) -> tuple[datetime, ...]:
    """Timestamps within 24 h of the running max timestamp, sorted."""
    if dna.t_last is None:
        return ()
    return tuple(dna.recent_ts.irange(minimum=dna.t_last - RECENT_WINDOW))


@dataclass(frozen=True, slots=True)
class StatsView:
    w: float
    mean: float
    var: float


def _close(a: float, b: float, rel: float, abs_: float) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_)


@dataclass(frozen=True, slots=True)
class DNAView:
    """Read-only snapshot of an account's Behavioural_DNA handed to Sensor_Agents."""

    account: str
    count: int
    t_last: datetime | None
    amount: StatsView
    iat: StatsView
    hour_hist: tuple[float, ...]
    weekday_hist: tuple[float, ...]
    channel_counts: Mapping[str, float]
    channel_t: datetime | None
    last_location: tuple[float, float, datetime] | None
    counterparties: frozenset[str]
    recent_ts: tuple[datetime, ...]

    @staticmethod
    def of(dna: BehaviouralDNA) -> DNAView:
        return DNAView(
            account=dna.account,
            count=dna.count,
            t_last=dna.t_last,
            amount=StatsView(dna.amount.w, dna.amount.mean, dna.amount.var),
            iat=StatsView(dna.iat.w, dna.iat.mean, dna.iat.var),
            hour_hist=tuple(float(x) for x in dna.hour_hist),
            weekday_hist=tuple(float(x) for x in dna.weekday_hist),
            channel_counts=MappingProxyType(dict(dna.channel_counts)),
            channel_t=dna.channel_t,
            last_location=dna.last_location,
            counterparties=frozenset(dna.counterparties),
            recent_ts=recent_window(dna),
        )

    @staticmethod
    def empty(account: str) -> DNAView:
        return DNAView.of(BehaviouralDNA(account))

    def recent_count(self, since: datetime) -> int:
        """Number of recent timestamps at or after ``since`` (Velocity helper)."""
        return len(self.recent_ts) - bisect_left(self.recent_ts, since)

    def isclose(self, other: DNAView, rel: float = 1e-6, abs_: float = 1e-9) -> bool:
        """Structural equality with float fields compared within tolerance."""
        if (
            self.account != other.account
            or self.count != other.count
            or self.t_last != other.t_last
            or self.channel_t != other.channel_t
            or self.last_location != other.last_location
            or self.counterparties != other.counterparties
            or self.recent_ts != other.recent_ts
            or set(self.channel_counts) != set(other.channel_counts)
        ):
            return False
        for s, o in ((self.amount, other.amount), (self.iat, other.iat)):
            if not (
                _close(s.w, o.w, rel, abs_)
                and _close(s.mean, o.mean, rel, abs_)
                and _close(s.var, o.var, rel, abs_)
            ):
                return False
        pairs = list(zip(self.hour_hist, other.hour_hist)) + list(
            zip(self.weekday_hist, other.weekday_hist)
        )
        pairs += [(v, other.channel_counts[k]) for k, v in self.channel_counts.items()]
        return all(_close(a, b, rel, abs_) for a, b in pairs)
