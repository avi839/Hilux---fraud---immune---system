"""Unit tests for helix.sim.generator (Synthetic_Generator, Req 3.1-3.12)."""

from __future__ import annotations

import json
import math
from datetime import timedelta
from itertools import islice

import numpy as np
import pytest

from helix.codec import OPTIONAL_FIELDS, format
from helix.config import ConfigValidationError
from helix.config.schema import GeneratorConfig
from helix.sim import GTSource, InMemoryGroundTruth, ManualClock, SyntheticGenerator
from helix.sim.generator import CITIES

US = timedelta(microseconds=1)


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def make(seed=7, **kw):
    gt = InMemoryGroundTruth()
    clock = ManualClock()
    return SyntheticGenerator(GeneratorConfig(**kw), clock, gt, seed=seed), gt, clock


# --- Req 3.1: profiles ---------------------------------------------------------


def test_profiles_have_required_shape():
    gen, _, _ = make(accounts=1_000)
    assert len(gen.profiles) == 1_000
    assert len({p.account_id for p in gen.profiles}) == 1_000
    for p in gen.profiles:
        assert 1 <= len(p.counterparties) <= 50
        assert p.account_id not in p.counterparties
        assert len(set(p.counterparties)) == len(p.counterparties)
        assert 6 <= p.active_len <= 14  # 1,000 accounts cover every hour without extension
        assert bin(p.active_mask).count("1") == p.active_len
        assert math.isclose(sum(p.channel_mix), 1.0)
        assert 0.3 <= p.sigma <= 0.8
        _, clat, clon = CITIES[p.home_city]
        assert haversine_km(p.home_lat, p.home_lon, clat, clon) <= 10.5


def test_single_account_covers_every_hour_and_never_pays_itself():
    gen, _, _ = make(accounts=1, change_fraction=0.0)
    (p,) = gen.profiles
    assert p.active_mask == (1 << 24) - 1
    for ev in islice(gen, 500):
        assert ev.src != ev.dst


# --- Req 3.3 / 3.4: determinism and default seed ------------------------------


def test_default_seed_used_when_seed_is_none():
    a, _, _ = make(seed=None, accounts=50, default_seed=1234)
    b, _, _ = make(seed=1234, accounts=50)
    assert a.seed == 1234
    assert list(islice(a, 300)) == list(islice(b, 300))


def test_different_seeds_differ():
    a, _, _ = make(seed=1, accounts=50)
    b, _, _ = make(seed=2, accounts=50)
    assert [e.src for e in islice(a, 50)] != [e.src for e in islice(b, 50)]


def test_events_until_chunking_matches_iteration():
    a, _, clock = make(accounts=40, rate_eps=20)
    chunked = []
    for step_ms in (130, 7, 2_000, 1, 999, 5_000):
        clock.advance(timedelta(milliseconds=step_ms))
        chunked += list(a.events_until())  # defaults to clock.now()
        assert all(e.ts <= clock.now() for e in chunked)
    b, _, _ = make(accounts=40, rate_eps=20)
    assert chunked == list(islice(b, len(chunked)))
    assert len(chunked) > 100
    nxt = next(a)
    assert nxt.ts > clock.now()


# --- Req 3.5: rate within ±10% in every 10 s window after a 5 s warm-up -------


@pytest.mark.parametrize("rate,seconds", [(1, 600), (1.37, 600), (7.3, 200), (50, 120), (1_000, 30)])
def test_every_10s_window_within_10_percent(rate, seconds):
    gen, _, _ = make(accounts=200, rate_eps=rate)
    end = gen.t0 + timedelta(seconds=seconds)
    ts = np.array([(e.ts - gen.t0) // US for e in gen.events_until(end)], dtype=np.int64)
    assert np.all(np.diff(ts) > 0)
    w, warm = 10_000_000, 5_000_000
    # count(s) only changes at s = t_k (point leaves) and s = t_k - 10 s (point enters).
    starts = np.concatenate([ts, ts + 1, ts - w])
    starts = starts[(starts >= warm) & (starts + w <= ts[-1])]
    counts = np.searchsorted(ts, starts + w, "left") - np.searchsorted(ts, starts, "left")
    assert counts.min() >= 9 * rate and counts.max() <= 11 * rate, (counts.min(), counts.max())


# --- Req 3.2: >= 95% in profile while no change is in effect ------------------


def _in_profile_fractions(gen, events):
    by_acct = {p.account_id: (i, p) for i, p in enumerate(gen.profiles)}
    tot: dict[str, int] = {}
    ok: dict[str, int] = {}
    for e in events:
        i, p = by_acct[e.src]
        ch = gen.changes.get(i)
        if ch is not None and ch.in_effect(e.ts):
            continue
        good = (
            p.is_active(e.ts.hour)
            and haversine_km(e.lat, e.lon, p.home_lat, p.home_lon) <= 50
            and e.dst in p.counterparties
        )
        tot[e.src] = tot.get(e.src, 0) + 1
        ok[e.src] = ok.get(e.src, 0) + int(good)
    return {a: ok[a] / tot[a] for a in tot}, tot


def test_95_percent_in_profile_over_a_full_day():
    gen, _, _ = make(accounts=30, rate_eps=1, change_fraction=0.2, omit_frac=0.0)
    events = list(gen.events_until(gen.t0 + timedelta(hours=24)))
    fracs, tot = _in_profile_fractions(gen, events)
    # Permanent changes start within the first hour, so judge accounts with enough no-change events.
    fracs = {a: f for a, f in fracs.items() if tot[a] >= 100}
    assert len(fracs) >= 24
    assert min(fracs.values()) >= 0.95, min(fracs.values())
    # Some off-hours noise exists, so the guarantee is not met vacuously.
    assert min(fracs.values()) < 1.0


# --- Req 3.6-3.8: behaviour changes -------------------------------------------


@pytest.mark.parametrize("n,frac,expected", [(10, 0.05, 1), (30, 0.05, 2), (1_000, 0.05, 50), (7, 0.0, 0), (5, 1.0, 5)])
def test_change_count_rounds_halves_up(n, frac, expected):
    gen, gt, _ = make(accounts=n, change_fraction=frac)
    assert len(gen.changes) == expected
    assert len(gt.records) == expected
    assert len({r.event_id for r in gt.records}) == expected  # exactly one per account


def test_changes_recorded_before_first_affected_event():
    gen, gt, clock = make(accounts=20, rate_eps=5, change_fraction=1.0)
    assert {r.event_id for r in gt.records} == {p.account_id for p in gen.profiles}
    for r in gt.records:
        assert r.label == "LEGIT_CHANGE" and r.source is GTSource.GENERATOR
        assert r.change_kind in ("relocation", "salary", "new_payee", "travel")
        assert r.written_at == clock.now() and r.effective_at > r.written_at
    first = next(gen)
    assert all(r.written_at <= first.ts for r in gt.records)


def test_travel_reverts_and_relocation_persists():
    gen, _, _ = make(seed=3, accounts=40, rate_eps=5, change_fraction=1.0, omit_frac=0.0, travel_duration_h=1)
    kinds = {c.kind for c in gen.changes.values()}
    assert {"travel", "relocation"} <= kinds
    events = list(gen.events_until(gen.t0 + timedelta(hours=3)))
    idx = {p.account_id: i for i, p in enumerate(gen.profiles)}
    seen = {"during": 0, "after": 0, "moved": 0}
    for e in events:
        i = idx[e.src]
        p, ch = gen.profiles[i], gen.changes[i]
        d_home = haversine_km(e.lat, e.lon, p.home_lat, p.home_lon)
        if ch.kind == "travel":
            if ch.in_effect(e.ts):
                assert haversine_km(e.lat, e.lon, ch.lat, ch.lon) <= 50 < d_home
                seen["during"] += 1
            elif e.ts >= ch.end:
                assert d_home <= 50
                seen["after"] += 1
        elif ch.kind == "relocation" and e.ts >= ch.start:
            assert haversine_km(e.lat, e.lon, ch.lat, ch.lon) <= 50 < d_home
            seen["moved"] += 1
    assert all(v > 0 for v in seen.values()), seen


def test_new_payee_is_new_and_used_after_start():
    gen, _, _ = make(seed=5, accounts=40, rate_eps=5, change_fraction=1.0)
    payees = {c.account_id: c for c in gen.changes.values() if c.kind == "new_payee"}
    assert payees
    for c in payees.values():
        p = gen.profiles[c.account]
        assert c.new_payee not in p.counterparties and c.new_payee != p.account_id
    used_before = used_after = 0
    for e in gen.events_until(gen.t0 + timedelta(hours=2)):
        c = payees.get(e.src)
        if c is not None and e.dst == c.new_payee:
            if e.ts < c.start:
                used_before += 1
            else:
                used_after += 1
    assert used_before == 0 and used_after > 0


# --- Req 3.9 / 3.10 / 3.11: omission, absence, validity -----------------------


@pytest.mark.parametrize("omit", [0.0, 0.1, 0.35])
def test_omission_fraction_within_2pp_over_10k(omit):
    gen, _, _ = make(accounts=100, omit_frac=omit)
    events = list(islice(gen, 10_000))
    for f in OPTIONAL_FIELDS:
        frac = sum(getattr(e, f) is None for e in events) / len(events)
        assert abs(frac - omit) <= 0.02, (f, frac)
    for e in events[:500]:
        doc = json.loads(format(e))
        for f in OPTIONAL_FIELDS:
            assert (f in doc) == (getattr(e, f) is not None)
            if f in doc:
                assert doc[f] not in ("", None)
        assert e.src != e.dst


# --- Req 3.12: config validation ----------------------------------------------


def test_refuses_to_start_naming_every_bad_setting():
    cfg = GeneratorConfig(accounts=0, rate_eps=5_000, change_fraction=1.5, omit_frac=-0.1, travel_duration_h=0)
    gt = InMemoryGroundTruth()
    with pytest.raises(ConfigValidationError) as ei:
        SyntheticGenerator(cfg, ManualClock(), gt, seed=2**32)
    paths = {e.path for e in ei.value.errors}
    assert paths == {
        "generator.accounts",
        "generator.rate_eps",
        "generator.change_fraction",
        "generator.omit_frac",
        "generator.travel_duration_h",
        "seed",
    }
    assert gt.records == []
    for p in paths:
        assert p in str(ei.value)


def test_bad_default_seed_and_nan_rejected():
    with pytest.raises(ConfigValidationError) as ei:
        SyntheticGenerator(GeneratorConfig(default_seed=-1, rate_eps=float("nan")), ManualClock(), InMemoryGroundTruth())
    assert {e.path for e in ei.value.errors} == {"generator.default_seed", "generator.rate_eps"}


@pytest.mark.parametrize("seed", [0, 2**32 - 1])
def test_seed_bounds_accepted(seed):
    gen, _, _ = make(seed=seed, accounts=5)
    assert next(gen).tx_id.startswith(f"GEN-{seed}-")
