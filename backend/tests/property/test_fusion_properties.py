"""Property tests for helix.fusion (design Properties 17-23)."""

from __future__ import annotations

import math
import random
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.fusion import (
    VACUOUS,
    Contribution,
    ContributorType,
    FusedAssessment,
    Mass,
    conjunctive,
    discount,
    fuse,
    is_valid,
    normalise,
)

TOL = 1e-9
BOUNDARY = 1e-12  # float vs exact K may straddle the threshold only this close to it

unit = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@st.composite
def masses(draw: st.DrawFn) -> Mass:
    if draw(st.booleans()):
        return draw(st.sampled_from([VACUOUS, Mass(1.0, 0.0, 0.0), Mass(0.0, 1.0, 0.0), Mass(0.5, 0.5, 0.0)]))
    a, b = sorted((draw(unit), draw(unit)))
    return Mass(a, b - a, 1.0 - b)


weights = st.one_of(st.sampled_from([0.0, 1.0]), unit)
thresholds = st.one_of(st.sampled_from([0.95, 1.0, 0.5]), st.floats(min_value=0.0, max_value=1.0, exclude_min=True))
kinds = st.sampled_from(list(ContributorType))


@st.composite
def contributions(draw: st.DrawFn, max_size: int = 6, prefix: str = "c") -> list[Contribution]:
    ids = draw(st.lists(st.integers(0, 99), unique=True, max_size=max_size))
    return [Contribution(f"{prefix}{i:02d}", draw(kinds), draw(masses()), draw(weights)) for i in ids]


@st.composite
def invalid_contributions(draw: st.DrawFn) -> list[Contribution]:
    bad_mass = st.sampled_from(
        [Mass(-0.1, 0.6, 0.5), Mass(0.9, 0.9, 0.0), Mass(math.nan, 0.0, 1.0), Mass(0.2, 0.2, 0.2), Mass(math.inf, 0, 0)]
    )
    bad_weight = st.sampled_from([-0.01, 1.01, math.nan, math.inf, -math.inf])
    out = []
    for i in range(draw(st.integers(1, 4))):
        if draw(st.booleans()):
            out.append(Contribution(f"bad{i}", draw(kinds), draw(bad_mass), draw(weights)))
        else:
            out.append(Contribution(f"bad{i}", draw(kinds), draw(masses()), draw(bad_weight)))
    return out


def close(a: FusedAssessment, b: FusedAssessment) -> bool:
    return a.high_conflict == b.high_conflict and all(
        abs(x - y) <= TOL
        for x, y in [
            (a.mass.f, b.mass.f),
            (a.mass.l, b.mass.l),
            (a.mass.t, b.mass.t),
            (a.belief, b.belief),
            (a.plausibility, b.plausibility),
            (a.uncertainty, b.uncertainty),
            (a.conflict, b.conflict),
        ]
    )


def exact_fuse(cs: list[Contribution], hc: float) -> tuple[tuple[Fraction, Fraction, Fraction], Fraction, bool]:
    """Naive exact reference: discount, conjunctive combination, K, Dempster or Yager."""
    one = Fraction(1)
    F, L, T, E = Fraction(0), Fraction(0), one, Fraction(0)
    for c in cs:
        w = Fraction(c.weight)
        f, l, t = w * Fraction(c.mass.f), w * Fraction(c.mass.l), one - w + w * Fraction(c.mass.t)  # noqa: E741
        # m(A) for the product of focal sets; ∅ collects {F}∩{L}.
        F, L, T, E = F * f + F * t + T * f, L * l + L * t + T * l, T * t, E + F * l + L * f
    K = E
    if K < 1 and K <= Fraction(hc):
        return (F / (one - K), L / (one - K), T / (one - K)), K, False
    return (F, L, T + K), K, True


# Feature: helix-fraud-immune-system, Property 17: Fusion validity
@pytest.mark.property
@given(cs=contributions(), hc=thresholds)
def test_fusion_validity(cs: list[Contribution], hc: float) -> None:
    fa, errs = fuse(cs, hc, tx_id="tx")
    assert errs == []
    assert is_valid(fa.mass)
    for x in (fa.mass.f, fa.mass.l, fa.mass.t, fa.belief, fa.plausibility, fa.uncertainty, fa.conflict):
        assert 0.0 <= x <= 1.0
    assert abs(math.fsum((fa.mass.f, fa.mass.l, fa.mass.t)) - 1.0) <= TOL
    assert fa.belief <= fa.plausibility
    assert abs((fa.plausibility - fa.belief) - fa.uncertainty) <= TOL


# Feature: helix-fraud-immune-system, Property 18: Fusion agrees with an exact reference
@pytest.mark.property
@given(cs=contributions(), hc=thresholds)
def test_fusion_matches_exact_reference(cs: list[Contribution], hc: float) -> None:
    fa, _ = fuse(cs, hc)
    (f, l, t), K, hc_flag = exact_fuse(cs, hc)  # noqa: E741
    assert abs(fa.conflict - float(K)) <= TOL
    if fa.high_conflict != hc_flag:
        # Only a K within rounding of the threshold may land on the other branch.
        assert abs(float(K) - hc) <= BOUNDARY
        return
    assert abs(fa.mass.f - float(f)) <= TOL
    assert abs(fa.mass.l - float(l)) <= TOL
    assert abs(fa.mass.t - float(t)) <= TOL


# Feature: helix-fraud-immune-system, Property 19: Fusion confluence
@pytest.mark.property
@given(cs=contributions(), hc=thresholds, perm=st.randoms(use_true_random=False))
def test_fusion_confluence(cs: list[Contribution], hc: float, perm: random.Random) -> None:
    shuffled = list(cs)
    perm.shuffle(shuffled)
    # Production fuse sorts its inputs: bit-identical for every permutation.
    assert fuse(shuffled, hc) == fuse(cs, hc)
    # Raw folds in each order agree within tolerance.
    raw = [normalise(*conjunctive(discount(c.mass, c.weight) for c in order), hc) for order in (cs, shuffled)]
    (m1, k1, h1), (m2, k2, h2) = raw
    assert abs(k1 - k2) <= TOL
    if h1 != h2:
        assert abs(k1 - hc) <= BOUNDARY
        return
    assert abs(m1.f - m2.f) <= TOL and abs(m1.l - m2.l) <= TOL and abs(m1.t - m2.t) <= TOL


# Feature: helix-fraud-immune-system, Property 20: Vacuous identity
@pytest.mark.property
@given(m=masses(), hc=thresholds, kind=kinds)
def test_vacuous_identity(m: Mass, hc: float, kind: ContributorType) -> None:
    fa, _ = fuse([Contribution("m", kind, m, 1.0), Contribution("v", kind, VACUOUS, 1.0)], hc)
    assert fa.conflict == 0.0
    assert not fa.high_conflict
    assert abs(fa.mass.f - m.f) <= TOL and abs(fa.mass.l - m.l) <= TOL and abs(fa.mass.t - m.t) <= TOL


# Feature: helix-fraud-immune-system, Property 21: Zero weight equals omission
@pytest.mark.property
@given(cs=contributions(), extra=masses(), kind=kinds, hc=thresholds, pos=st.integers(0, 6))
def test_zero_weight_equals_omission(
    cs: list[Contribution], extra: Mass, kind: ContributorType, hc: float, pos: int
) -> None:
    with_zero = list(cs)
    with_zero.insert(min(pos, len(cs)), Contribution("zero", kind, extra, 0.0))
    a, _ = fuse(with_zero, hc)
    b, _ = fuse(cs, hc)
    assert close(a, b)


# Feature: helix-fraud-immune-system, Property 22: Empty input fuses to vacuous
@pytest.mark.property
@given(cs=contributions(), hc=thresholds)
def test_empty_or_zero_weight_fuses_to_vacuous(cs: list[Contribution], hc: float) -> None:
    zeroed = [Contribution(c.contributor_id, c.kind, c.mass, 0.0) for c in cs]
    fa, _ = fuse(zeroed, hc)
    assert (fa.belief, fa.plausibility, fa.uncertainty, fa.conflict) == (0.0, 1.0, 1.0, 0.0)
    assert fa.mass == VACUOUS and not fa.high_conflict


# Feature: helix-fraud-immune-system, Property 23: Invalid contributions are excluded
@pytest.mark.property
@given(cs=contributions(), bad=invalid_contributions(), hc=thresholds, data=st.data())
def test_invalid_contributions_excluded(
    cs: list[Contribution], bad: list[Contribution], hc: float, data: st.DataObject
) -> None:
    mixed = list(cs)
    for b in bad:
        mixed.insert(data.draw(st.integers(0, len(mixed))), b)
    fa, errs = fuse(mixed, hc, tx_id="tx42")
    ref, ref_errs = fuse(cs, hc, tx_id="tx42")
    assert ref_errs == []
    assert fa == ref
    assert sorted(e.contributor_id for e in errs) == sorted(b.contributor_id for b in bad)
    assert all(e.tx_id == "tx42" and e.reason for e in errs)
