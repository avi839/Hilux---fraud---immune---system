"""Coverage_Matrix (Req 30.6-30.8): per HELIX_Technique, who detects it and how well.

Design decisions:
- Detecting contributors are the *deployed* ones: the pipeline's Sensor_Agents (by name, from
  ``helix.stage1.sensors``; all built-in sensors when no pipeline is given), every Motif_Detector,
  every *active* Detection_Rule version and every active Antibody in the pool. Each is mapped to
  techniques with ``helix.soc.techniques`` (Req 30.2).
- A technique with no detecting contributor is flagged ``gap`` (Req 30.7).
- The Attacker_Agent detection rate of a technique is the mean of ``1 - evasion`` over the
  attacker's completed, scored campaigns whose Pattern_Genome type maps to the technique
  (``genome_techniques``). ``evasion`` is computed by the attacker from the Evaluation_Monitor's
  detected-events callback, so this module never reads the Ground_Truth_Store. ``None`` when no
  scored campaign exercised the technique. Honest note: Req 30.6 names the Evaluation_Monitor as
  the owner of this computation; it lives here so the monitor stays untouched, and the auth-leg
  variant of an ATO campaign is not recorded on the campaign, so ATO campaigns count only towards
  HX-T0006.
- The payload carries the catalogue version and the "ATT&CK-inspired, not official MITRE data"
  notice (Req 30.8).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .techniques import (
    Catalogue,
    ContributorMapping,
    antibody_techniques,
    default_catalogue,
    default_mapping,
    genome_techniques,
    motif_techniques,
    sensor_names,
    sensor_techniques,
)


@dataclass(frozen=True, slots=True)
class Detector:
    """One deployed contributor and the techniques it is mapped to."""

    contributor_id: str
    kind: str  # SENSOR | MOTIF | RULE | ANTIBODY
    techniques: tuple[str, ...]
    version: Optional[int] = None  # Detection_Rule version

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"contributor_id": self.contributor_id, "kind": self.kind}
        if self.version is not None:
            d["version"] = self.version
        return d


@dataclass(frozen=True, slots=True)
class CoverageRow:
    technique_id: str
    name: str
    domain: str
    kill_chain_stage: str
    contributors: tuple[Detector, ...]
    detection_rate: Optional[float]  # Attacker_Agent detection rate, None without data
    campaigns: int  # scored attacker campaigns behind detection_rate

    @property
    def gap(self) -> bool:
        return not self.contributors

    def to_dict(self) -> dict[str, Any]:
        return {"technique_id": self.technique_id, "name": self.name, "domain": self.domain,
                "kill_chain_stage": self.kill_chain_stage,
                "contributors": [c.to_dict() for c in self.contributors],
                "detection_rate": self.detection_rate, "campaigns": self.campaigns, "gap": self.gap}


@dataclass(frozen=True, slots=True)
class CoverageMatrix:
    catalogue_version: str
    notice: str
    rows: tuple[CoverageRow, ...]
    generated_at: Optional[datetime] = None

    @property
    def gaps(self) -> tuple[str, ...]:
        return tuple(r.technique_id for r in self.rows if r.gap)

    def row(self, technique_id: str) -> CoverageRow:
        return next(r for r in self.rows if r.technique_id == technique_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalogue_version": self.catalogue_version,
            "notice": self.notice,
            "generated_at": None if self.generated_at is None else self.generated_at.isoformat(),
            "summary": {"techniques": len(self.rows), "covered": len(self.rows) - len(self.gaps),
                        "gaps": len(self.gaps)},
            "gaps": list(self.gaps),
            "techniques": [r.to_dict() for r in self.rows],
        }


# --- deployed contributors ---------------------------------------------------------------


def builtin_detectors(sensors: Optional[Iterable[str]] = None,
                      mapping: Optional[ContributorMapping] = None) -> list[Detector]:
    """Sensor_Agents (``sensors`` names, default all built-in) and every Motif_Detector."""
    mp = mapping or default_mapping()
    out = [Detector(n, "SENSOR", sensor_techniques(n, mp)) for n in (sensor_names() if sensors is None else sensors)]
    from helix.motifs import MotifType, contributor_id

    out += [Detector(contributor_id(t), "MOTIF", motif_techniques(t, mp)) for t in MotifType]
    return out


def rule_detectors(rules: Any) -> list[Detector]:
    """Active Detection_Rule versions of a ``RuleEngine`` (duck-typed: ``active_rules()``)."""
    if rules is None:
        return []
    return [Detector(r.contributor_id, "RULE", tuple(sorted(r.techniques)), r.version) for r in rules.active_rules()]


def antibody_detectors(immune: Any, catalogue: Optional[Catalogue] = None) -> list[Detector]:
    """Active Antibodies of an Immune_System (duck-typed: ``pool()`` of objects with
    ``contributor_id``, ``tags`` and ``active``)."""
    if immune is None:
        return []
    out = []
    for ab in immune.pool():
        if getattr(ab, "active", True):
            out.append(Detector(ab.contributor_id, "ANTIBODY", antibody_techniques(ab.tags, catalogue)))
    return out


def pipeline_detectors(helix: Any = None, rules: Any = None, immune: Any = None,
                       catalogue: Optional[Catalogue] = None,
                       mapping: Optional[ContributorMapping] = None) -> list[Detector]:
    sensors = None
    if helix is not None:
        stage1 = getattr(helix, "stage1", None)
        names = [str(s.name) for s in getattr(stage1, "sensors", ())]
        sensors = names or None
        rules = rules if rules is not None else getattr(helix, "rules", None)
        immune = immune if immune is not None else getattr(helix, "immune", None)
    return [*builtin_detectors(sensors, mapping), *rule_detectors(rules), *antibody_detectors(immune, catalogue)]


# --- attacker detection rate --------------------------------------------------------------


def attacker_rates(attacker: Any) -> dict[str, tuple[float, int]]:
    """``technique_id -> (detection rate, scored campaigns)`` from an Attacker_Agent
    (duck-typed: ``completed_campaigns()`` with ``genome_id`` / ``evasion`` and ``genome(id)``
    with ``ptype``)."""
    if attacker is None:
        return {}
    sums: dict[str, list[float]] = {}
    for c in attacker.completed_campaigns():
        ev = getattr(c, "evasion", None)
        if ev is None:
            continue
        try:
            techs = genome_techniques(attacker.genome(c.genome_id).ptype)
        except (KeyError, ValueError):
            continue
        for t in techs:
            sums.setdefault(t, []).append(1.0 - float(ev))
    return {t: (sum(v) / len(v), len(v)) for t, v in sums.items()}


def rates_from_outcomes(outcomes: Iterable[tuple[Iterable[str], float]]) -> dict[str, tuple[float, int]]:
    """Same as ``attacker_rates`` from ``(technique ids, detected fraction)`` pairs."""
    sums: dict[str, list[float]] = {}
    for techs, det in outcomes:
        for t in set(techs):
            sums.setdefault(t, []).append(float(det))
    return {t: (sum(v) / len(v), len(v)) for t, v in sums.items()}


# --- the matrix ------------------------------------------------------------------------------


def coverage_matrix(
    detectors: Iterable[Detector],
    rates: Optional[Mapping[str, tuple[float, int]]] = None,
    catalogue: Optional[Catalogue] = None,
    generated_at: Optional[datetime] = None,
) -> CoverageMatrix:
    """One row per catalogue technique (id order): detecting contributors (sorted by id),
    attacker detection rate and the gap flag (Req 30.6, 30.7)."""
    cat = catalogue or default_catalogue()
    rates = rates or {}
    by_tech: dict[str, dict[str, Detector]] = {t.technique_id: {} for t in cat}
    for d in detectors:
        for t in d.techniques:
            if t in by_tech:
                by_tech[t][d.contributor_id] = d
    rows = []
    for t in cat:
        rate, n = rates.get(t.technique_id, (None, 0))
        rows.append(CoverageRow(
            technique_id=t.technique_id, name=t.name, domain=t.domain.value,
            kill_chain_stage=t.kill_chain_stage.value,
            contributors=tuple(by_tech[t.technique_id][k] for k in sorted(by_tech[t.technique_id])),
            detection_rate=rate, campaigns=n,
        ))
    return CoverageMatrix(cat.version, cat.notice, tuple(rows), generated_at)


def pipeline_coverage(helix: Any = None, *, rules: Any = None, immune: Any = None, attacker: Any = None,
                      catalogue: Optional[Catalogue] = None,
                      mapping: Optional[ContributorMapping] = None) -> CoverageMatrix:
    """Coverage_Matrix of a running pipeline (any of the parts may be None)."""
    now = None
    if helix is not None and getattr(helix, "clock", None) is not None:
        now = helix.clock.now()
    dets = pipeline_detectors(helix, rules, immune, catalogue, mapping)
    return coverage_matrix(dets, attacker_rates(attacker), catalogue, now)


__all__ = [
    "CoverageMatrix",
    "CoverageRow",
    "Detector",
    "antibody_detectors",
    "attacker_rates",
    "builtin_detectors",
    "coverage_matrix",
    "pipeline_coverage",
    "pipeline_detectors",
    "rates_from_outcomes",
    "rule_detectors",
]
