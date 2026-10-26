"""Property tests for helix.risk (design Properties 25-27)."""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.fusion import VACUOUS, Contribution, ContributorType, Mass, fuse
from helix.risk import (
    DEFAULT_BANDS,
    TIER_ORDER,
    Band,
    SeverityTier,
    case_risk,
    risk,
    tier,
    tier_rank,
    validate_bands,
)

unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
thresholds = st.one_of(st.sampled_from([0.95, 1.0, 0.5]), st.floats(min_value=0.0, max_value=1.0, exclude_min=True))


@st.composite
def masses(draw: st.DrawFn) -> Mass:
    if draw(st.booleans()):
        return draw(st.sampled_from([VACUOUS, Mass(1.0, 0.0, 0.0), Mass(0.0, 1.0, 0.0), Mass(0.5, 0.5, 0.0)]))
    a, b = sorted((draw(unit), draw(unit)))
    return Mass(a, b - a, 1.0 - b)


@st.composite
def contributions(draw: st.DrawFn, kind: ContributorType, prefix: str, max_size: int = 4) -> list[Contribution]:
    ids = draw(st.lists(st.integers(0, 99), unique=True, max_size=max_size))
    return [Contribution(f"{prefix}{i:02d}", kind, draw(masses()), draw(unit)) for i in ids]


@st.composite
def tx_assessments(draw: st.DrawFn):
    hc = draw(thresholds)
    n = draw(st.integers(1, 5))
    return [fuse(draw(contributions(ContributorType.SENSOR, "s")), hc, tx_id=f"tx{i}")[0] for i in range(n)]


@st.composite
def valid_bands(draw: st.DrawFn) -> tuple[float, tuple[Band, ...]]:
    if draw(st.booleans()):
        return 0.6, DEFAULT_BANDS
    a = draw(st.floats(min_value=0.0, max_value=0.99))
    cuts = draw(
        st.lists(st.floats(min_value=a, max_value=1.0, exclude_min=True, exclude_max=True), min_size=3, max_size=3, unique=True)
    )
    edges = [a, *sorted(cuts), 1.0]
    return a, tuple(Band(tier=t, lo=edges[k], hi=edges[k + 1]) for k, t in enumerate(TIER_ORDER))


# Feature: helix-fraud-immune-system, Property 25: Risk is the pignistic transform
@pytest.mark.property
@given(cs=contributions(ContributorType.SENSOR, "s", max_size=6), hc=thresholds)
def test_risk_is_pignistic(cs: list[Contribution], hc: float) -> None:
    """**Validates: Requirements 13.1**"""
    fa, _ = fuse(cs, hc)
    r = risk(fa)
    assert 0.0 <= r <= 1.0
    assert abs(r - (fa.belief + 0.5 * fa.uncertainty)) <= 1e-12


# Feature: helix-fraud-immune-system, Property 25: Risk is the pignistic transform
@pytest.mark.property
@given(txs=tx_assessments(), motifs=contributions(ContributorType.MOTIF, "m"), hc=thresholds)
def test_case_risk_vs_max_event_risk(txs, motifs: list[Contribution], hc: float) -> None:
    """**Validates: Requirements 13.2, 13.3**"""
    top = max(risk(fa) for fa in txs)
    assert case_risk(txs, [], hc) == top
    s = case_risk(txs, motifs, hc)
    assert 0.0 <= s <= 1.0
    assert s >= top
    if not motifs:
        assert s == top


# Feature: helix-fraud-immune-system, Property 26: Severity monotonicity and band boundaries
@pytest.mark.property
@given(cfg=valid_bands(), data=st.data())
def test_severity_monotone_and_band_boundaries(cfg: tuple[float, tuple[Band, ...]], data: st.DataObject) -> None:
    """**Validates: Requirements 13.7, 13.8**"""
    a, bands = cfg
    assert validate_bands(bands, a) == []

    s1, s2 = sorted((data.draw(st.floats(a, 1.0)), data.draw(st.floats(a, 1.0))))
    assert tier_rank(tier(s1, bands)) <= tier_rank(tier(s2, bands))

    for k, b in enumerate(bands):
        assert tier(b.lo, bands) is b.tier
        below = math.nextafter(b.lo, -math.inf)
        if below >= 0.0:
            assert tier(below, bands) is (bands[k - 1].tier if k else SeverityTier.LOW)
    assert tier(1.0, bands) is SeverityTier.CRITICAL


GRID = (0.0, 0.25, 0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 1.0)
grid = st.sampled_from(GRID)
tiers = st.sampled_from(list(SeverityTier))


def reference_valid(bands: Sequence[Band], a: float) -> bool:
    return (
        tuple(b.tier for b in bands) == TIER_ORDER
        and all(b.lo < b.hi for b in bands)
        and all(x.hi == y.lo for x, y in zip(bands, bands[1:]))
        and bands[0].lo == a
        and bands[-1].hi == 1.0
    )


@st.composite
def band_configs(draw: st.DrawFn) -> tuple[float, list[Band]]:
    a, valid = draw(valid_bands())
    bands = list(valid)
    op = draw(st.sampled_from(["none", "swap", "lo", "hi", "drop", "dup", "threshold", "random"]))
    k = draw(st.integers(0, len(bands) - 1))
    if op == "swap":
        j = draw(st.integers(0, len(bands) - 1))
        bands[k], bands[j] = bands[j], bands[k]
    elif op == "lo":
        bands[k] = Band(tier=bands[k].tier, lo=draw(st.one_of(grid, unit)), hi=bands[k].hi)
    elif op == "hi":
        bands[k] = Band(tier=bands[k].tier, lo=bands[k].lo, hi=draw(st.one_of(grid, unit)))
    elif op == "drop":
        del bands[k]
    elif op == "dup":
        bands.insert(k, bands[k])
    elif op == "threshold":
        a = draw(st.one_of(grid, unit))
    elif op == "random":
        a = draw(grid)
        bands = draw(st.lists(st.builds(Band, tier=tiers, lo=grid, hi=grid), max_size=5))
    return a, bands


# Feature: helix-fraud-immune-system, Property 27: Band validation
@pytest.mark.property
@given(cfg=band_configs())
def test_band_validation(cfg: tuple[float, list[Band]]) -> None:
    """**Validates: Requirements 13.11**"""
    a, bands = cfg
    errs = validate_bands(bands, a)
    if bands and reference_valid(bands, a):
        assert errs == []
    else:
        assert errs, "invalid bands must be reported"
        assert all(e.path == "risk.bands" for e in errs)
