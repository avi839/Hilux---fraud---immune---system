"""Property tests for helix.motifs against the brute-force oracle (design Properties 39-41)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import assume, event, given
from hypothesis import strategies as st

from helix.fusion import Mass
from helix.graph import Edge, TemporalGraph
from helix.motifs import DEFAULT_MOTIF_MASSES, MotifEngine, MotifType
from tests.oracles import motif_oracle as oracle

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
RT = Decimal("10000")
ACCOUNTS = [f"A{i}" for i in range(12)]
BG_ACCOUNTS = ACCOUNTS[:7]  # denser background so random cycles/chains occur
AMOUNTS = [Decimal(x) for x in ("100", "800", "850", "900", "1000", "4500", "5000", "9000", "9500", "9999.99", "10000")]


@dataclass(frozen=True)
class Ev:
    tx_id: str
    ts: datetime
    src: str
    dst: str
    amount: Decimal
    currency: str = "EUR"


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def cents(c: int) -> Decimal:
    return Decimal(c) / 100


# ---------------------------------------------------------------- generators


@st.composite
def background(draw, n_max: int = 30, horizon_min: int = 3 * 1440) -> list[Ev]:
    n = draw(st.integers(0, n_max))
    out = []
    for i in range(n):
        out.append(
            Ev(
                f"bg{i}",
                at(draw(st.integers(0, horizon_min // 10)) * 10),
                draw(st.sampled_from(BG_ACCOUNTS)),
                draw(st.sampled_from(BG_ACCOUNTS)),
                draw(st.sampled_from(AMOUNTS)),
            )
        )
    return out


@st.composite
def injected(draw) -> tuple[MotifType, list[Ev]]:
    """One instance satisfying its glossary definition."""
    kind = draw(st.sampled_from(list(MotifType)))
    base = draw(st.integers(0, 3 * 1440))
    accts = draw(st.permutations(ACCOUNTS))
    amt = lambda: draw(st.sampled_from(AMOUNTS))  # noqa: E731
    evs: list[Ev] = []
    if kind == MotifType.ROUND_TRIP:
        k = draw(st.integers(2, 6))
        ts = base
        for i in range(k):
            evs.append(Ev(f"inj{i}", at(ts), accts[i], accts[(i + 1) % k], amt()))
            ts += draw(st.integers(1, 72 * 60 // 5))  # k - 1 gaps <= 72 h in total
    elif kind in (MotifType.FAN_OUT, MotifType.FAN_IN):
        hub, cps = accts[0], accts[1 : 1 + draw(st.integers(8, 9))]
        for i, cp in enumerate(cps):
            ts = at(base + draw(st.integers(0, 1440)))
            src, dst = (hub, cp) if kind == MotifType.FAN_OUT else (cp, hub)
            evs.append(Ev(f"inj{i}", ts, src, dst, amt()))
    elif kind == MotifType.STRUCTURING:
        for i in range(draw(st.integers(3, 4))):
            evs.append(
                Ev(f"inj{i}", at(base + draw(st.integers(0, 2880))), accts[0], draw(st.sampled_from(accts[1:])),
                   cents(draw(st.integers(900_000, 999_999))))
            )
    else:  # MULE_CHAIN
        k = draw(st.integers(3, 5))
        c, ts = draw(st.integers(10_000, 1_000_000)), base
        for i in range(k - 1):
            evs.append(Ev(f"inj{i}", at(ts), accts[i], accts[i + 1], cents(c)))
            ts += draw(st.integers(0, 60))
            c = draw(st.integers(-(-c * 8 // 10), c * 12 // 10))  # next >= 0.8 * previous
    return kind, evs


def to_edge(e: Ev) -> Edge:
    return Edge(e.tx_id, e.src, e.dst, e.ts, e.amount, e.currency)


def run(events, g: TemporalGraph, engine: MotifEngine, on_step=None):
    emitted, truncated = [], False
    for e in events:
        if not g.add(e):
            continue
        r = engine.on_edge(to_edge(e), g)
        truncated |= r.is_truncated
        emitted.extend(r.instances)
        if on_step is not None:
            on_step(e, r)
    return emitted, truncated


# ---------------------------------------------------------------- Property 39


# Feature: helix-fraud-immune-system, Property 39: Motif completeness
@pytest.mark.property
@given(bg=background(), inj=injected(), data=st.data())
def test_motif_completeness(bg, inj, data):
    """**Validates: Requirements 11.1, 11.8**"""
    kind, inj_evs = inj
    arrival = data.draw(st.permutations(bg + inj_evs))  # arbitrary order => many late edges
    g = TemporalGraph()  # 7 d window; all ts within 6 d, so nothing is evicted or rejected
    emitted, truncated = run(arrival, g, MotifEngine(budget_ms=1000))
    event("truncated" if truncated else "complete search")
    assume(not truncated)

    by_type: dict[MotifType, set[frozenset[str]]] = {t: set() for t in MotifType}
    for i in emitted:
        by_type[i.motif_type].add(i.tx_ids)

    want = frozenset(e.tx_id for e in inj_evs)
    assert any(want <= s for s in by_type[kind]), (kind, want)

    # Full brute-force completeness over the final edge set.
    edges = g.edges()
    assert len(edges) == len(arrival)
    assert oracle.all_cycles(edges) <= by_type[MotifType.ROUND_TRIP]
    assert oracle.all_mule_chains(edges) <= by_type[MotifType.MULE_CHAIN]
    for t, windows in (
        (MotifType.FAN_OUT, oracle.fan_windows(edges, True)),
        (MotifType.FAN_IN, oracle.fan_windows(edges, False)),
        (MotifType.STRUCTURING, oracle.structuring_windows(edges, RT)),
    ):
        for w in windows:
            assert any(w <= s for s in by_type[t]), (t, w)


# ---------------------------------------------------------------- Properties 40 and 41


@st.composite
def noisy_stream(draw):
    """Background + optional injection, shuffled, with replayed duplicates mixed in.
    Spans 4 days against a 30 h graph window, so eviction and too-old rejections occur."""
    evs = draw(background(n_max=35, horizon_min=4 * 1440)) + draw(injected())[1]
    arrival = list(draw(st.permutations(evs)))
    for _ in range(draw(st.integers(0, 6)) if evs else 0):
        dup = draw(st.sampled_from(evs))
        arrival.insert(draw(st.integers(0, len(arrival))), dup)
    return arrival


SMALL_WINDOW_S = 30 * 3600.0


# Feature: helix-fraud-immune-system, Property 40: Motif soundness
@pytest.mark.property
@given(arrival=noisy_stream())
def test_motif_soundness(arrival):
    """**Validates: Requirements 11.2, 11.3, 11.4, 11.5, 11.7, 11.9**"""
    g = TemporalGraph(window_s=SMALL_WINDOW_S)
    engine = MotifEngine(budget_ms=1000)

    def check(e: Ev, r) -> None:
        present = {x.tx_id: x for x in g.edges()}
        snapshot = list(present.values())
        cycles = mules = None
        for inst in r.instances:
            event(f"emitted {inst.motif_type.value}")
            assert e.tx_id in inst.tx_ids  # Req 11.1: anchored on the new edge
            assert inst.tx_ids <= present.keys()
            assert {x.tx_id for x in inst.edges} == inst.tx_ids and len(inst.edges) == len(inst.tx_ids)
            assert all(present[x.tx_id] == x for x in inst.edges)
            edges = list(inst.edges)
            assert oracle.satisfies(inst.motif_type.value, edges, RT), inst
            assert inst.ts_start == min(x.ts for x in edges) and inst.ts_end == max(x.ts for x in edges)
            m = DEFAULT_MOTIF_MASSES[inst.motif_type]
            assert inst.mass == Mass(m, 0.0, 1.0 - m)
            t = inst.motif_type
            if t == MotifType.ROUND_TRIP:
                cycles = oracle.all_cycles(snapshot) if cycles is None else cycles
                assert inst.tx_ids in cycles
                assert 2 <= len(set(inst.accounts)) == len(inst.accounts) <= 6
                assert all(a.ts < b.ts for a, b in zip(edges, edges[1:]))
                assert inst.accounts == tuple(x.src for x in edges)
                assert inst.duration_s <= 72 * 3600
            elif t == MotifType.MULE_CHAIN:
                mules = oracle.all_mule_chains(snapshot) if mules is None else mules
                assert inst.tx_ids in mules
                assert len(inst.accounts) >= 3 and inst.accounts == (*(x.src for x in edges), edges[-1].dst)
                assert all(rt >= 0.8 - 1e-12 for rt in inst.detail["forwarding_ratios"])
                assert all(0 <= d <= 3600 for d in inst.detail["hop_delays_s"])
            elif t in (MotifType.FAN_OUT, MotifType.FAN_IN):
                assert inst.duration_s <= 24 * 3600
                assert len(inst.detail["counterparties"]) >= 8
            else:
                assert inst.duration_s <= 48 * 3600
                assert inst.detail["reporting_threshold"] == str(RT)
                assert all(oracle.in_band(Decimal(a), RT) for a in inst.detail["amounts"])

    run(arrival, g, engine, on_step=check)


# Feature: helix-fraud-immune-system, Property 41: No duplicate motif emission
@pytest.mark.property
@given(arrival=noisy_stream(), replays=st.lists(st.integers(0, 10_000), max_size=10))
def test_no_duplicate_motif_emission(arrival, replays):
    """**Validates: Requirements 11.11**"""
    g = TemporalGraph(window_s=SMALL_WINDOW_S)
    engine = MotifEngine(budget_ms=1000)
    keys: list[tuple[MotifType, frozenset[str]]] = []

    def record(e: Ev, r) -> None:
        keys.extend((i.motif_type, i.tx_ids) for i in r.instances)
        # Replay: re-invoke the engine for edges already in the graph, as a re-delivered
        # event would; nothing new may be emitted for an already-seen (type, tx set).
        present = g.edges()
        for j in replays[:3]:
            if present:
                r2 = engine.on_edge(present[j % len(present)], g)
                keys.extend((i.motif_type, i.tx_ids) for i in r2.instances)

    run(arrival, g, engine, on_step=record)
    assert len(keys) == len(set(keys))
