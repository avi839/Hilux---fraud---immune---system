"""Label_Suggester (design §26, Req 41.4, 41.5): a pure Suggested_Label from a Case's fused assessment.

With ``b = Bel(FRAUD)``, ``p = Pl(FRAUD)`` and ``u = p - b``:

- ``b >= suggest_fraud_belief`` -> CONFIRMED_FRAUD, confidence ``b``;
- ``b <= suggest_fp_belief``    -> FALSE_POSITIVE, confidence ``1 - b``;
- otherwise                    -> NONE, confidence 0.

The reason is a fixed template stating Belief, Plausibility and Uncertainty_Mass (2 dp). On
an alerted Case a FALSE_POSITIVE suggestion means "the score is driven by missing evidence",
not "the evidence shows legitimate behaviour": ``1 - b`` counts unknown mass, so the reason
always prints ``u`` beside it.

The suggestion is computed at serialisation and never stored; it never changes Case state.
Only an Investigator submission (``Helix.label`` / ``Helix.label_bulk``) writes ``case.label``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from helix.config.schema import TriageConfig

SuggestionLabel = Literal["CONFIRMED_FRAUD", "FALSE_POSITIVE", "NONE"]
#: FALSE_POSITIVE < NONE < CONFIRMED_FRAUD (Property 88 monotonicity order)
RANK: dict[str, int] = {"FALSE_POSITIVE": 0, "NONE": 1, "CONFIRMED_FRAUD": 2}
NO_ASSESSMENT = "no fused assessment is available for this case"


@dataclass(frozen=True, slots=True)
class SuggestedLabel:
    label: SuggestionLabel
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "confidence": self.confidence, "reason": self.reason}


def suggest_from(belief: float, plausibility: float, cfg: TriageConfig) -> SuggestedLabel:
    """The §26 rule on ``Bel(FRAUD)`` / ``Pl(FRAUD)``."""
    b = min(1.0, max(0.0, float(belief)))
    p = min(1.0, max(b, float(plausibility)))
    u = p - b
    head = f"Belief {b:.2f}, plausibility {p:.2f}, unknown {u:.2f}"
    if b >= cfg.suggest_fraud_belief:
        return SuggestedLabel("CONFIRMED_FRAUD", b, f"{head}: committed evidence supports fraud")
    if b <= cfg.suggest_fp_belief:
        return SuggestedLabel("FALSE_POSITIVE", 1.0 - b,
                              f"{head}: little committed fraud evidence; most of the risk is unknown (u={u:.2f})")
    return SuggestedLabel("NONE", 0.0, f"{head}: evidence is between the suggestion thresholds")


def suggest(fa: Any, cfg: TriageConfig) -> SuggestedLabel:
    """Suggested_Label of a ``FusedAssessment`` (anything with ``belief`` / ``plausibility``)."""
    return suggest_from(fa.belief, fa.plausibility, cfg)


def case_assessment(case: Any) -> Optional[Any]:
    """The Case's current assessment: stage 2 when Motifs are attached, else the best event."""
    cr = getattr(case, "case_risk", None)
    if cr is not None:
        return cr.stage2 if cr.stage2 is not None else cr.best_tx
    return getattr(case, "best_fa", None)


def suggest_case(case: Any, cfg: TriageConfig) -> SuggestedLabel:
    """Suggested_Label of a Case; NONE (confidence 0) when it has no assessment yet (e.g. a
    Case restored from Storage before its first recompute)."""
    fa = case_assessment(case)
    if fa is None:
        return SuggestedLabel("NONE", 0.0, NO_ASSESSMENT)
    return suggest(fa, cfg)


__all__ = ["NO_ASSESSMENT", "RANK", "SuggestedLabel", "case_assessment", "suggest", "suggest_case", "suggest_from"]
