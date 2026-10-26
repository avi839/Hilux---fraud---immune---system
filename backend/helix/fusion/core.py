"""Pure Dempster-Shafer Evidence_Fuser on the frame {FRAUD, LEGIT} (design §5).

No I/O and no state. Focal sets are {F}, {L} and Θ = {F, L}.

Numerics:
- Contributions are sorted before folding, so any input permutation gives
  bit-identical output through ``fuse``.
- Dempster normalisation divides by ``F + L + T`` (equal to ``1 - K`` in exact
  arithmetic). Every fold term is a sum of non-negative products, so this
  denominator keeps small relative error even when K is close to 1, unlike
  ``1 - K`` which cancels catastrophically.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Literal

TOL = 1e-9


class ContributorType(str, Enum):
    SENSOR = "SENSOR"
    ANTIBODY = "ANTIBODY"
    MOTIF = "MOTIF"
    FUSED = "FUSED"
    RULE = "RULE"  # Detection_Rule (Increment 4, helix.soc.rules; only with soc.enabled)
    INDICATOR = "INDICATOR"  # threat-intel Indicator match (Increment 4, helix.soc.intel)

    def __str__(self) -> str:  # StrEnum-like behaviour on Python 3.10
        return self.value


@dataclass(frozen=True, slots=True)
class Mass:
    """Mass function: f = m({F}), l = m({L}), t = m(Θ). Valid iff ``is_valid``."""

    f: float
    l: float  # noqa: E741 - name fixed by the design
    t: float


VACUOUS = Mass(0.0, 0.0, 1.0)


@dataclass(frozen=True, slots=True)
class Contribution:
    contributor_id: str
    kind: ContributorType
    mass: Mass
    weight: float  # Sensor_Weight in [0, 1]


@dataclass(frozen=True, slots=True)
class FusedAssessment:
    tx_id: str
    stage: Literal[1, 2]
    mass: Mass  # normalised (Dempster) or Yager result
    belief: float  # = mass.f
    plausibility: float  # = mass.f + mass.t
    uncertainty: float  # = mass.t
    conflict: float  # K, unreduced
    high_conflict: bool
    contributions: tuple[Contribution, ...]  # valid, undiscounted, in fold order


@dataclass(frozen=True, slots=True)
class FusionError:
    contributor_id: str
    tx_id: str
    reason: str


def _unit(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and 0.0 <= x <= 1.0


def is_valid(m: object) -> bool:
    """Each mass finite in [0, 1] and ``fsum == 1`` within 1e-9."""
    if not isinstance(m, Mass):
        return False
    if not (_unit(m.f) and _unit(m.l) and _unit(m.t)):
        return False
    return abs(math.fsum((m.f, m.l, m.t)) - 1.0) <= TOL


def discount(m: Mass, w: float) -> Mass:
    """Shafer discounting: ``(w·f, w·l, 1 - w + w·t)``."""
    return Mass(w * m.f, w * m.l, 1.0 - w + w * m.t)


def conjunctive(ms: Iterable[Mass]) -> tuple[float, float, float, float]:
    """Unnormalised conjunctive fold; returns ``(F, L, T, E)`` with E = m(∅)."""
    F, L, T, E = 0.0, 0.0, 1.0, 0.0
    for m in ms:
        f, l, t = m.f, m.l, m.t  # noqa: E741
        F, L, T, E = (
            F * (f + t) + T * f,
            L * (l + t) + T * l,
            T * t,
            E + F * l + L * f,
        )
    return F, L, T, E


def normalise(F: float, L: float, T: float, E: float, high_conflict: float) -> tuple[Mass, float, bool]:
    """Apply Dempster (``K < 1 and K <= high_conflict``) or the Yager fallback once.

    Returns ``(mass, K, is_high_conflict)``; K is reported unreduced.
    """
    K = E
    s = F + L + T
    if K < 1.0 and K <= high_conflict and s > 0.0:
        return Mass(F / s, L / s, T / s), K, False
    return Mass(F, L, min(1.0, T + K)), K, True


def _invalid_reason(c: object) -> str | None:
    if not isinstance(c, Contribution):
        return f"not a Contribution: {type(c).__name__}"
    if not is_valid(c.mass):
        return f"invalid mass: {c.mass!r}"
    if not _unit(c.weight):
        return f"weight outside [0, 1]: {c.weight!r}"
    return None


def _sort_key(c: Contribution) -> tuple[str, str, float, float, float, float]:
    # contributor_id first (design); the rest only breaks ties between duplicate ids.
    return (c.contributor_id, str(c.kind), c.mass.f, c.mass.l, c.mass.t, float(c.weight))


def _assessment(
    tx_id: str, stage: Literal[1, 2], mass: Mass, K: float, hc: bool, cs: tuple[Contribution, ...]
) -> FusedAssessment:
    return FusedAssessment(
        tx_id=tx_id,
        stage=stage,
        mass=mass,
        belief=mass.f,
        plausibility=min(1.0, mass.f + mass.t),
        uncertainty=mass.t,
        conflict=K,
        high_conflict=hc,
        contributions=cs,
    )


def fuse(
    cs: Sequence[Contribution],
    high_conflict: float,
    *,
    tx_id: str = "",
    stage: Literal[1, 2] = 1,
) -> tuple[FusedAssessment, list[FusionError]]:
    """Validate, discount, fold and normalise contributions for one event.

    Invalid contributions (bad mass or weight outside [0, 1]) are excluded and
    returned as ``FusionError``s naming the contributor and the event.
    """
    if not (_unit(high_conflict) and high_conflict > 0.0):
        raise ValueError(f"fusion.high_conflict must be in (0, 1], got {high_conflict!r}")

    errors: list[FusionError] = []
    valid: list[Contribution] = []
    for c in cs:
        reason = _invalid_reason(c)
        if reason is None:
            valid.append(c)
        else:
            cid = c.contributor_id if isinstance(c, Contribution) else "<unknown>"
            errors.append(FusionError(cid, tx_id, reason))

    ordered = tuple(sorted(valid, key=_sort_key))
    F, L, T, E = conjunctive(discount(c.mass, float(c.weight)) for c in ordered)
    mass, K, hc = normalise(F, L, T, E, high_conflict)
    return _assessment(tx_id, stage, mass, K, hc, ordered), errors


def fuse_stage2(
    stage1: FusedAssessment, motif_contribs: Sequence[Contribution], high_conflict: float
) -> tuple[FusedAssessment, list[FusionError]]:
    """Stage 2: the stage-1 result at weight 1 plus Motif contributions, same ``fuse``."""
    seed = Contribution("stage1", ContributorType.FUSED, stage1.mass, 1.0)
    return fuse([seed, *motif_contribs], high_conflict, tx_id=stage1.tx_id, stage=2)
