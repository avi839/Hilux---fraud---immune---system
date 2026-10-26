"""Unit tests for helix.fusion (design §5, Req 6, 7.5)."""

from __future__ import annotations

import math

import pytest

from helix.fusion import (
    VACUOUS,
    Contribution,
    ContributorType,
    FusionError,
    Mass,
    conjunctive,
    discount,
    fuse,
    fuse_stage2,
    is_valid,
)

S = ContributorType.SENSOR


def c(cid: str, f: float, l: float, t: float, w: float = 1.0, kind: ContributorType = S) -> Contribution:  # noqa: E741
    return Contribution(cid, kind, Mass(f, l, t), w)


def approx_mass(m: Mass, f: float, l: float, t: float) -> None:  # noqa: E741
    assert m.f == pytest.approx(f, abs=1e-12)
    assert m.l == pytest.approx(l, abs=1e-12)
    assert m.t == pytest.approx(t, abs=1e-12)


@pytest.mark.parametrize(
    "m, ok",
    [
        (VACUOUS, True),
        (Mass(1.0, 0.0, 0.0), True),
        (Mass(0.1, 0.2, 0.7), True),
        (Mass(0.5, 0.5, 1e-10), True),  # within 1e-9 of 1
        (Mass(0.5, 0.5, 1e-8), False),
        (Mass(-0.1, 0.6, 0.5), False),
        (Mass(1.1, 0.0, -0.1), False),
        (Mass(math.nan, 0.0, 1.0), False),
        (Mass(math.inf, 0.0, 0.0), False),
        ((0.0, 0.0, 1.0), False),
    ],
)
def test_is_valid(m: object, ok: bool) -> None:
    assert is_valid(m) is ok


def test_discount_formula() -> None:
    approx_mass(discount(Mass(0.6, 0.1, 0.3), 0.5), 0.3, 0.05, 0.65)
    assert discount(Mass(0.6, 0.1, 0.3), 0.0) == VACUOUS
    assert discount(Mass(0.6, 0.1, 0.3), 1.0) == Mass(0.6, 0.1, 0.3)


def test_conjunctive_two_masses_by_hand() -> None:
    F, L, T, E = conjunctive([Mass(0.6, 0.1, 0.3), Mass(0.5, 0.2, 0.3)])
    assert (F, L, T, E) == pytest.approx((0.63, 0.11, 0.09, 0.17), abs=1e-12)
    assert conjunctive([]) == (0.0, 0.0, 1.0, 0.0)


def test_dempster_normalisation() -> None:
    fa, errs = fuse([c("a", 0.6, 0.1, 0.3), c("b", 0.5, 0.2, 0.3)], 0.95, tx_id="tx1")
    assert errs == []
    approx_mass(fa.mass, 0.63 / 0.83, 0.11 / 0.83, 0.09 / 0.83)
    assert fa.conflict == pytest.approx(0.17)
    assert not fa.high_conflict
    assert fa.belief == fa.mass.f and fa.uncertainty == fa.mass.t
    assert fa.plausibility == pytest.approx(fa.mass.f + fa.mass.t)
    assert fa.tx_id == "tx1" and fa.stage == 1


def test_threshold_is_inclusive_for_dempster() -> None:
    cs = [c("a", 0.5, 0.0, 0.5), c("b", 0.0, 1.0, 0.0)]  # K = 0.5
    fa, _ = fuse(cs, 0.5)
    assert not fa.high_conflict
    approx_mass(fa.mass, 0.0, 1.0, 0.0)
    fa, _ = fuse(cs, 0.4)
    assert fa.high_conflict
    assert fa.conflict == pytest.approx(0.5)
    approx_mass(fa.mass, 0.0, 0.5, 0.5)  # Yager: K moved to Θ


def test_total_conflict_uses_yager_even_at_threshold_one() -> None:
    fa, _ = fuse([c("a", 1.0, 0.0, 0.0), c("b", 0.0, 1.0, 0.0)], 1.0)
    assert fa.high_conflict
    assert fa.conflict == 1.0
    assert fa.mass == VACUOUS


@pytest.mark.parametrize("cs", [[], [c("a", 0.9, 0.0, 0.1, w=0.0), c("b", 0.0, 1.0, 0.0, w=0.0)]])
def test_empty_or_all_zero_weight_is_vacuous(cs: list[Contribution]) -> None:
    fa, errs = fuse(cs, 0.95)
    assert errs == []
    assert (fa.belief, fa.plausibility, fa.uncertainty, fa.conflict) == (0.0, 1.0, 1.0, 0.0)
    assert not fa.high_conflict


def test_invalid_contributions_excluded_with_errors() -> None:
    good = [c("a", 0.6, 0.1, 0.3), c("b", 0.5, 0.2, 0.3)]
    bad = [c("bad_mass", 0.9, 0.9, 0.0), c("bad_w", 0.1, 0.1, 0.8, w=1.5), c("nan_w", 0.1, 0.1, 0.8, w=math.nan)]
    fa, errs = fuse([bad[0], good[0], bad[1], good[1], bad[2]], 0.95, tx_id="tx9")
    ref, _ = fuse(good, 0.95, tx_id="tx9")
    assert fa == ref
    assert [e.contributor_id for e in errs] == ["bad_mass", "bad_w", "nan_w"]
    assert all(isinstance(e, FusionError) and e.tx_id == "tx9" and e.reason for e in errs)


def test_order_is_irrelevant_bitwise() -> None:
    cs = [c("z", 0.2, 0.3, 0.5, 0.7), c("a", 0.6, 0.1, 0.3, 0.9), c("m", 0.0, 0.8, 0.2, 0.4)]
    assert fuse(cs, 0.95) == fuse(cs[::-1], 0.95)
    assert [x.contributor_id for x in fuse(cs, 0.95)[0].contributions] == ["a", "m", "z"]


def test_stage2_reuses_fuse() -> None:
    s1, _ = fuse([c("a", 0.6, 0.1, 0.3), c("b", 0.5, 0.2, 0.3)], 0.95, tx_id="tx1")
    motifs = [c("fan_in", 0.7, 0.0, 0.3, kind=ContributorType.MOTIF)]
    s2, errs = fuse_stage2(s1, motifs, 0.95)
    seed = Contribution("stage1", ContributorType.FUSED, s1.mass, 1.0)
    ref, _ = fuse([seed, *motifs], 0.95, tx_id="tx1", stage=2)
    assert errs == [] and s2 == ref
    assert s2.stage == 2 and s2.tx_id == "tx1"
    assert s2.belief > s1.belief


@pytest.mark.parametrize("hc", [0.0, -0.1, 1.0001, math.nan])
def test_bad_high_conflict_threshold_rejected(hc: float) -> None:
    with pytest.raises(ValueError, match="high_conflict"):
        fuse([], hc)


def test_replacing_with_vacuous_never_lowers_uncertainty() -> None:
    # Req 7.5 lemma: missing fields only turn some masses vacuous.
    full = [c("a", 0.6, 0.1, 0.3), c("b", 0.2, 0.5, 0.3, 0.8), c("c", 0.4, 0.4, 0.2, 0.6)]
    base, _ = fuse(full, 0.95)
    for i in range(len(full)):
        reduced = [x if j != i else Contribution(x.contributor_id, x.kind, VACUOUS, x.weight) for j, x in enumerate(full)]
        fa, _ = fuse(reduced, 0.95)
        assert not fa.high_conflict
        assert fa.uncertainty >= base.uncertainty - 1e-9
