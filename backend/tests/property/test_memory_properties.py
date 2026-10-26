"""Property tests for helix.memory (design Properties 29-32)."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.memory import DNAView, ProfileStore

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
CHANNELS = ("card", "web", "mobile")
CANARIES = frozenset({"CANARY-1"})


@dataclass(frozen=True, slots=True)
class Ev:  # minimal stand-in satisfying helix.memory.EventLike
    tx_id: str
    ts: datetime
    src: str
    dst: str
    amount: Decimal
    lat: float | None = None
    lon: float | None = None
    channel: str | None = None
    device_id: str | None = None


@st.composite
def histories(draw, min_size=1, max_size=60, srcs=("A",), dsts=("B", "C", "D", "E")):
    n = draw(st.integers(min_size, max_size))
    t = T0
    out = []
    for i in range(n):
        # Mostly forward steps, some backwards (late / out-of-order) and some ties.
        t = t + timedelta(seconds=draw(st.integers(-2 * 86_400, 4 * 86_400)))
        has_loc = draw(st.booleans())
        out.append(
            Ev(
                tx_id=f"t{i}",
                ts=t,
                src=draw(st.sampled_from(srcs)),
                dst=draw(st.sampled_from(dsts)),
                amount=Decimal(draw(st.integers(1, 99_999_999_999))) / 100,
                lat=draw(st.floats(-90, 90)) if has_loc else None,
                lon=draw(st.floats(-180, 180)) if has_loc and draw(st.booleans()) else None,
                channel=draw(st.none() | st.sampled_from(CHANNELS)),
            )
        )
    return out


# ---- batch model (exact rational arithmetic over float weights) ---------------------
def _batch(xs: list[float], ws: list[float]) -> tuple[float, float, float]:
    if not xs:
        return 0.0, 0.0, 0.0
    W = sum(Fraction(w) for w in ws)
    S1 = sum(Fraction(w) * Fraction(x) for w, x in zip(ws, xs))
    S2 = sum(Fraction(w) * Fraction(x) ** 2 for w, x in zip(ws, xs))
    return float(W), float(S1 / W), float((S2 * W - S1 * S1) / (W * W))


def batch_model(events: list[Ev], h_s: float):
    T: list[datetime] = []
    for ev in events:
        T.append(ev.ts if not T or ev.ts > T[-1] else T[-1])
    Tn = T[-1]
    w = [0.5 ** ((Tn - Ti).total_seconds() / h_s) for Ti in T]
    amount = _batch([float(e.amount) for e in events], w)
    iat = _batch([(T[i] - T[i - 1]).total_seconds() for i in range(1, len(T))], w[1:])
    hour = [0.0] * 24
    for e, wi in zip(events, w):
        hour[e.ts.hour] += wi
    return amount, iat, hour


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9 if b == 0 else 0.0)


def _check_online_vs_batch(events: list[Ev], half_life_days: float) -> None:
    store = ProfileStore(half_life_days=half_life_days)
    for ev in events:
        before = store.view(ev.src)
        store.apply(ev)
        after = store.view(ev.src)
        if ev.channel is None:  # stats derived from a missing field are unchanged
            assert after.channel_counts == before.channel_counts
            assert after.channel_t == before.channel_t
        if ev.lat is None or ev.lon is None:
            assert after.last_location == before.last_location
    v = store.view(events[0].src)
    amount, iat, hour = batch_model(events, half_life_days * 86_400)
    for got, (w, mean, var) in ((v.amount, amount), (v.iat, iat)):
        assert _close(got.w, w), (got, w)
        assert _close(got.mean, mean), (got.mean, mean)
        assert _close(got.var, var), (got.var, var)
    for a, b in zip(v.hour_hist, hour):
        assert math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)


# Feature: helix-fraud-immune-system, Property 29: DNA online statistics equal batch
@pytest.mark.property
@given(events=histories(max_size=120), half_life_days=st.floats(1, 365))
def test_p29_online_equals_batch(events, half_life_days):
    """**Validates: Requirements 8.1, 8.3, 8.5, 8.6, 8.7**"""
    _check_online_vs_batch(events, half_life_days)


# Feature: helix-fraud-immune-system, Property 29: DNA online statistics equal batch
@pytest.mark.property
@settings(max_examples=8)
@given(seed=st.integers(0, 2**32 - 1), n=st.integers(1, 10_000), half_life_days=st.floats(1, 365))
def test_p29_online_equals_batch_long_sequences(seed, n, half_life_days):
    """Long sequences up to 10,000 events. **Validates: Requirements 8.1, 8.5, 8.6, 8.7**"""
    rng = random.Random(seed)
    t, events = T0, []
    for i in range(n):
        t += timedelta(seconds=rng.randint(-3_600, 6 * 3_600))
        has_loc = rng.random() < 0.9
        events.append(
            Ev(
                f"t{i}",
                t,
                "A",
                rng.choice("BCDE"),
                Decimal(rng.randint(1, 99_999_999_999)) / 100,
                rng.uniform(-90, 90) if has_loc else None,
                rng.uniform(-180, 180) if has_loc else None,
                rng.choice((None, *CHANNELS)),
            )
        )
    _check_online_vs_batch(events, half_life_days)


# Feature: helix-fraud-immune-system, Property 30: Sensors see only pre-event DNA
@pytest.mark.property
@given(history=histories(max_size=40, srcs=("A", "B", "C")), data=st.data())
def test_p30_sensors_see_only_pre_event_dna(history, data):
    """**Validates: Requirements 8.4, 8.14**"""
    *prior, nxt = history
    store = ProfileStore()
    for ev in prior:
        store.apply(ev)
    seen = store.view(nxt.src)  # what sensors receive

    own = [e for e in prior if e.src == nxt.src]
    reference = ProfileStore()
    for ev in own:
        reference.apply(ev)
    assert seen.isclose(reference.view(nxt.src), rel=0.0, abs_=0.0)
    if not own:
        assert seen.count == 0 and seen.counterparties == frozenset()

    store.apply(nxt)
    assert seen.isclose(reference.view(nxt.src), rel=0.0, abs_=0.0)  # snapshot is frozen
    fresh = data.draw(st.text("xyz", min_size=1, max_size=4).map(lambda s: "new-" + s))
    empty = store.view(fresh)
    assert empty.count == 0 and empty.counterparties == frozenset()


@st.composite
def withholding_cases(draw):
    events = draw(histories(min_size=1, max_size=50))
    horizon_days = draw(st.integers(1, 30))
    t_max = max(e.ts for e in events)
    eligible = [e.tx_id for e in events if e.ts >= t_max - timedelta(days=horizon_days)]
    S = draw(st.sets(st.sampled_from(eligible))) if eligible else set()
    upfront = draw(st.sets(st.sampled_from(sorted(S)))) if S else set()
    via_flag = draw(st.booleans())
    return events, horizon_days, S, upfront, via_flag


def _feed(store: ProfileStore, events, S, upfront, via_flag):
    """Apply ``events``; ``upfront`` ⊆ S is withheld on arrival, S - upfront removed after."""
    if not via_flag:
        store.withhold("A", upfront)
    for ev in events:
        store.apply(ev, withhold=via_flag and ev.tx_id in upfront)
    store.withhold("A", S - upfront)


# Feature: helix-fraud-immune-system, Property 31: Withholding equals never applying
@pytest.mark.property
@given(case=withholding_cases(), then_exclude=st.booleans())
def test_p31_withholding_equals_never_applying(case, then_exclude):
    """DNA part (CPD state is rebuilt via the on_rebuild hook, task 4.2).

    **Validates: Requirements 8.10, 8.11, 8.13**
    """
    events, horizon_days, S, upfront, via_flag = case
    store = ProfileStore(horizon_s=horizon_days * 86_400)
    _feed(store, events, S, upfront, via_flag)
    assert store.withheld("A") == frozenset(S)
    if then_exclude:
        store.exclude("A", S)
        assert store.withheld("A") == frozenset()

    never = ProfileStore(horizon_s=horizon_days * 86_400)
    for ev in events:
        if ev.tx_id not in S:
            never.apply(ev)
    assert store.view("A").isclose(never.view("A"))


# Feature: helix-fraud-immune-system, Property 32: Release equals timestamp-order application
@pytest.mark.property
@given(case=withholding_cases())
def test_p32_release_equals_timestamp_order_application(case):
    """**Validates: Requirements 8.12, 19.7**"""
    events, horizon_days, S, upfront, via_flag = case
    events = [  # route some destinations to a Canary
        Ev(e.tx_id, e.ts, e.src, "CANARY-1" if i % 5 == 4 else e.dst, e.amount, e.lat, e.lon, e.channel)
        for i, e in enumerate(events)
    ]
    store = ProfileStore(horizon_s=horizon_days * 86_400, is_canary=CANARIES.__contains__)
    _feed(store, events, S, upfront, via_flag)
    applied = store.release("A", S)
    assert store.withheld("A") == frozenset()

    expected = ProfileStore(horizon_s=horizon_days * 86_400)
    for ev in events:  # Req 33.6: Canary events never reach Behavioural_DNA, withheld or not
        if ev.tx_id not in S and ev.dst not in CANARIES:
            expected.apply(ev)
    order = sorted(
        (i, e) for i, e in enumerate(events) if e.tx_id in S and e.dst not in CANARIES
    )
    order.sort(key=lambda p: (p[1].ts, p[0]))
    for _, ev in order:
        expected.apply(ev)
    assert [e.tx_id for e in applied] == [e.tx_id for _, e in order]
    got = store.view("A")
    assert got.isclose(expected.view("A"))
    assert {e.dst for _, e in order} <= got.counterparties

    store.release("A", S)  # exactly once: a second release is a no-op
    assert store.view("A").isclose(got, rel=0.0, abs_=0.0)
