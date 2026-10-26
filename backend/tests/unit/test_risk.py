"""Unit tests for helix.risk (design §6, Req 13)."""

from __future__ import annotations

import math

import pytest

from helix.config import HelixConfig, with_updates
from helix.fusion import VACUOUS, Contribution, ContributorType, Mass, fuse, fuse_stage2
from helix.risk import (
    DEFAULT_BANDS,
    Band,
    SeverityTier,
    assess_case,
    case_risk,
    max_tier,
    risk,
    tier,
    tier_at_least,
    tier_rank,
    validate_bands,
)

L, M, H, C = SeverityTier.LOW, SeverityTier.MEDIUM, SeverityTier.HIGH, SeverityTier.CRITICAL


def fa(tx_id: str, mass: Mass, w: float = 1.0):
    out, errs = fuse([Contribution("s", ContributorType.SENSOR, mass, w)], 0.95, tx_id=tx_id)
    assert errs == []
    return out


def motif(cid: str, mass: Mass, w: float = 1.0) -> Contribution:
    return Contribution(cid, ContributorType.MOTIF, mass, w)


# --- risk -------------------------------------------------------------------


def test_vacuous_risk_is_half():
    assert risk(fa("t", VACUOUS)) == 0.5
    assert risk(fuse([], 0.95)[0]) == 0.5


def test_risk_is_belief_plus_half_uncertainty():
    a = fa("t", Mass(0.6, 0.1, 0.3))
    assert risk(a) == pytest.approx(0.6 + 0.15)
    assert risk(fa("t", Mass(1.0, 0.0, 0.0))) == 1.0
    assert risk(fa("t", Mass(0.0, 1.0, 0.0))) == 0.0


# --- tier -------------------------------------------------------------------


@pytest.mark.parametrize(
    "score, expected",
    [
        (0.6, L),
        (0.65, L),
        (math.nextafter(0.7, 0.0), L),
        (0.7, M),
        (0.8, H),
        (math.nextafter(0.9, 0.0), H),
        (0.9, C),
        (0.99, C),
        (1.0, C),
    ],
)
def test_default_band_edges(score, expected):
    assert tier(score, DEFAULT_BANDS) is expected


def test_below_lowest_band_is_low():
    assert tier(0.3, DEFAULT_BANDS) is L
    assert tier(0.0, DEFAULT_BANDS) is L


@pytest.mark.parametrize("bad", [-0.01, 1.0000001, math.nan, math.inf])
def test_tier_rejects_out_of_range_scores(bad):
    with pytest.raises(ValueError):
        tier(bad, DEFAULT_BANDS)


def test_custom_bands():
    bands = (
        Band(tier=L, lo=0.5, hi=0.55),
        Band(tier=M, lo=0.55, hi=0.75),
        Band(tier=H, lo=0.75, hi=0.95),
        Band(tier=C, lo=0.95, hi=1.0),
    )
    assert validate_bands(bands, 0.5) == []
    assert [tier(s, bands) for s in (0.5, 0.6, 0.75, 0.96, 1.0)] == [L, M, H, C, C]


# --- tier ordering ----------------------------------------------------------


def test_tier_ordering_helpers():
    assert [tier_rank(t) for t in (L, M, H, C)] == [0, 1, 2, 3]
    assert tier_at_least(H, M) and tier_at_least(M, M) and not tier_at_least(L, M)
    assert max_tier([M, C, L]) is C
    assert max_tier([]) is L
    assert tier_rank("HIGH") == 2


# --- case risk --------------------------------------------------------------


def test_case_risk_without_motifs_is_max_tx_risk():
    txs = [fa("a", Mass(0.3, 0.2, 0.5)), fa("b", Mass(0.7, 0.0, 0.3)), fa("c", VACUOUS)]
    assert case_risk(txs, [], 0.95) == pytest.approx(0.85)
    r = assess_case(txs, [], 0.95)
    assert r.stage2 is None and r.best_tx.tx_id == "b"


def test_case_risk_with_motif_uses_stage2_of_best_tx():
    txs = [fa("a", Mass(0.3, 0.2, 0.5)), fa("b", Mass(0.5, 0.1, 0.4))]
    ms = [motif("fan_in:1", Mass(0.6, 0.0, 0.4))]
    expected_s2, _ = fuse_stage2(txs[1], ms, 0.95)
    r = assess_case(txs, ms, 0.95)
    assert r.best_tx.tx_id == "b"
    assert r.stage2 == expected_s2
    assert r.score == pytest.approx(max(risk(txs[1]), risk(expected_s2)))
    assert r.score > risk(txs[1])


def test_case_risk_motif_cannot_lower_below_max_tx_risk():
    txs = [fa("a", Mass(0.8, 0.0, 0.2))]
    legit_motif = [motif("m", Mass(0.0, 0.9, 0.1))]
    assert case_risk(txs, legit_motif, 0.95) == risk(txs[0])


def test_motif_created_case_can_score_below_threshold_and_maps_to_low():
    txs = [fa("a", Mass(0.0, 0.2, 0.8))]  # risk 0.4
    s = case_risk(txs, [motif("m", Mass(0.1, 0.0, 0.9))], 0.95)
    assert s < 0.6 and tier(s, DEFAULT_BANDS) is L


def test_case_risk_reports_invalid_motif_contribution():
    txs = [fa("a", Mass(0.5, 0.0, 0.5))]
    r = assess_case(txs, [motif("bad", Mass(0.9, 0.9, 0.0))], 0.95)
    assert [e.contributor_id for e in r.errors] == ["bad"]
    assert r.score == risk(txs[0])


def test_best_tx_tie_breaks_on_tx_id():
    txs = [fa("z", Mass(0.5, 0.0, 0.5)), fa("a", Mass(0.75, 0.25, 0.0))]  # both exactly 0.75
    assert assess_case(txs, [], 0.95).best_tx.tx_id == "a"


def test_case_risk_requires_an_assessment():
    with pytest.raises(ValueError):
        case_risk([], [], 0.95)


# --- band and threshold validation (Req 13.11, 13.12) -----------------------


def test_invalid_bands_reported_naming_band_settings():
    gap = (
        Band(tier=L, lo=0.6, hi=0.7),
        Band(tier=M, lo=0.71, hi=0.8),
        Band(tier=H, lo=0.8, hi=0.9),
        Band(tier=C, lo=0.9, hi=1.0),
    )
    errs = validate_bands(gap, 0.6)
    assert len(errs) == 1 and errs[0].path == "risk.bands"
    assert validate_bands(DEFAULT_BANDS, 0.5)[0].path == "risk.bands"


def test_alert_threshold_out_of_range_names_setting():
    cfg = with_updates(HelixConfig(), {"risk.alert_threshold": 1.5})
    assert [e.path for e in cfg.validate()] == ["risk.alert_threshold"]
