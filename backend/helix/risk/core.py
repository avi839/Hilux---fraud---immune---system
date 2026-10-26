"""Pure Risk_Score and Severity_Tier functions (design §6).

No I/O and no state. ``SeverityTier``, ``Band``, ``DEFAULT_BANDS`` and
``validate_bands`` live in ``helix.config`` and are re-exported from here.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from helix.config import TIER_ORDER, Band, SeverityTier
from helix.fusion import Contribution, FusedAssessment, FusionError, fuse_stage2

_RANK: dict[SeverityTier, int] = {t: i for i, t in enumerate(TIER_ORDER)}


# --- tier ordering (LOW < MEDIUM < HIGH < CRITICAL) -------------------------


def tier_rank(t: SeverityTier) -> int:
    """0 for LOW up to 3 for CRITICAL."""
    return _RANK[SeverityTier(t)]


def tier_at_least(a: SeverityTier, b: SeverityTier) -> bool:
    """True iff ``a`` is equal to or more severe than ``b``."""
    return tier_rank(a) >= tier_rank(b)


def max_tier(tiers: Iterable[SeverityTier]) -> SeverityTier:
    """Most severe tier; LOW for an empty iterable."""
    return max(tiers, key=tier_rank, default=SeverityTier.LOW)


# --- risk ---------------------------------------------------------------------


def risk(fa: FusedAssessment) -> float:
    """Pignistic BetP(F) = Bel + 0.5·U (Req 13.1). Vacuous gives 0.5.

    Clamped to [0, 1] only to absorb float rounding on a valid mass.
    """
    return min(1.0, max(0.0, fa.belief + 0.5 * fa.uncertainty))


def tier(score: float, bands: Sequence[Band]) -> SeverityTier:
    """Tier whose half-open band ``[lo, hi)`` contains ``score`` (Req 13.7).

    The last (CRITICAL) band also contains 1.0. A score below the lowest band,
    which only a motif-created Case can have, maps to LOW. ``bands`` are
    assumed valid (``validate_bands`` gates startup).
    """
    if not (isinstance(score, (int, float)) and math.isfinite(score) and 0.0 <= score <= 1.0):
        raise ValueError(f"risk score must be finite in [0, 1], got {score!r}")
    if not bands or score < bands[0].lo:
        return SeverityTier.LOW
    for b in bands:
        if b.lo <= score < b.hi:
            return b.tier
    last = bands[-1]
    if score == last.hi == 1.0:
        return last.tier
    return SeverityTier.LOW  # unreachable for valid bands


# --- case risk ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseRisk:
    score: float  # Case Risk_Score in [0, 1]
    max_tx_risk: float
    best_tx: FusedAssessment  # highest-risk event, ties broken by smallest tx_id
    stage2: FusedAssessment | None  # None when no Motif is attached
    errors: tuple[FusionError, ...]  # invalid Motif contributions excluded by stage 2


def best_assessment(tx_assessments: Sequence[FusedAssessment]) -> FusedAssessment:
    """Highest-risk assessment; deterministic tie-break on the smallest tx_id."""
    if not tx_assessments:
        raise ValueError("a Case needs at least one Fused_Assessment")
    return min(tx_assessments, key=lambda fa: (-risk(fa), fa.tx_id))


def assess_case(
    tx_assessments: Sequence[FusedAssessment],
    motif_contribs: Sequence[Contribution],
    high_conflict: float,
) -> CaseRisk:
    """Case risk with its stage-2 assessment (Req 13.2, 13.3)."""
    best = best_assessment(tx_assessments)
    top = risk(best)
    if not motif_contribs:
        return CaseRisk(top, top, best, None, ())
    s2, errors = fuse_stage2(best, motif_contribs, high_conflict)
    return CaseRisk(max(top, risk(s2)), top, best, s2, tuple(errors))


def case_risk(
    tx_assessments: Sequence[FusedAssessment],
    motif_contribs: Sequence[Contribution],
    high_conflict: float,
) -> float:
    """``max(max_tx_risk, risk(stage2(best_tx, motifs)))``, or ``max_tx_risk`` without Motifs."""
    return assess_case(tx_assessments, motif_contribs, high_conflict).score
