"""Simulated investigator (demo only, opt-in with ``--sim-investigator``).

A stand-in for a human analyst so a live demo shows the LEARN half of the loop: every tick it
takes up to ``per_tick`` of the top Active_Queue Cases that are still OPEN (unassigned) and at
least ``min_age_s`` old, opens each as ``actor`` and submits a Feedback_Label through the
normal ``Helix.open_case`` / ``Helix.label`` path. The Feedback_Learner, Immune_System, SOC
Audit_Ledger, Indicator_Store and WorkloadMeter therefore react exactly as they would to a
human label (they are all ``Helix.feedback_hooks``).

The label comes from the generator / attacker ground truth: CONFIRMED_FRAUD when any of the
Case's transactions is labelled FRAUD, FALSE_POSITIVE otherwise, flipped with probability
``error_rate`` (seeded RNG, one draw per labelled Case) to model analyst mistakes.

Ground-truth isolation (Req 20.12, 24.11): this module lives in ``helix.evaluation`` because it
reads the ground-truth read side. The detection path never sees ground truth directly; it only
receives the Feedback_Label, as it would from a human.

Case age (``min_age_s``) is measured in **stream time** (``now - case.created_at`` on the
pipeline SimClock), so it is deterministic in tests; with the live generator stream time
advances at wall-clock pace, so the two agree in the demo. The tick interval is wall time.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from helix.cases import CaseStatus, Label

from .ground_truth import GroundTruthReader

log = logging.getLogger(__name__)

ACTOR = "sim-analyst"
NOTE = "simulated investigator (demo): label from generator ground truth"


@dataclass(frozen=True, slots=True)
class SimInvestigatorConfig:
    interval_s: float = 20.0  # wall-clock seconds between ticks
    per_tick: int = 2  # at most this many Cases labelled per tick
    min_age_s: float = 30.0  # stream-time age a Case must reach before it is picked
    error_rate: float = 0.05  # probability that the ground-truth label is flipped
    seed: int = 0
    actor: str = ACTOR

    def __post_init__(self) -> None:
        if not (self.interval_s > 0):
            raise ValueError("interval_s must be > 0")
        if self.per_tick < 1:
            raise ValueError("per_tick must be >= 1")
        if self.min_age_s < 0:
            raise ValueError("min_age_s must be >= 0")
        if not (0.0 <= self.error_rate <= 1.0):
            raise ValueError("error_rate must be in [0, 1]")
        if not self.actor:
            raise ValueError("actor must be non-empty")


@dataclass(frozen=True, slots=True)
class SimLabel:
    case_id: str
    label: Label
    truth_fraud: bool
    flipped: bool


class SimInvestigator:
    def __init__(self, helix: Any, reader: GroundTruthReader,
                 cfg: Optional[SimInvestigatorConfig] = None) -> None:
        self.helix = helix
        self.reader = reader
        self.cfg = cfg if cfg is not None else SimInvestigatorConfig()
        self._rng = random.Random(self.cfg.seed)
        self._fraud: set[str] = set()
        self._since: Optional[datetime] = None
        self.labels_total = 0
        self.confirmed = 0
        self.false_positive = 0
        self.flipped = 0
        self.errors = 0

    # --- ground truth ----------------------------------------------------------------

    def _refresh(self) -> None:
        """Pull new ground-truth records (``written_at >= last seen``, inclusive) into the
        fraud-id index."""
        latest = self._since
        for v in self.reader.labels_since(self._since):
            if v.is_fraud:
                self._fraud.add(v.event_id)
            if v.written_at is not None and (latest is None or v.written_at > latest):
                latest = v.written_at
        self._since = latest

    def is_fraud_case(self, case: Any) -> bool:
        return any(tx in self._fraud for tx in case.tx_ids)

    # --- one tick --------------------------------------------------------------------

    def candidates(self, now: datetime) -> list[Any]:
        """The top Active_Queue Cases (priority order) that are OPEN, unassigned and at least
        ``min_age_s`` old in stream time; at most ``per_tick``."""
        cm = self.helix.cases
        out: list[Any] = []
        for cid in self.helix.prioritiser.active_ids():
            case = cm.cases.get(cid)
            if case is None or case.status is not CaseStatus.OPEN or case.assignee is not None:
                continue
            created = case.created_at
            if created is not None and (now - created).total_seconds() < self.cfg.min_age_s:
                continue
            out.append(case)
            if len(out) >= self.cfg.per_tick:
                break
        return out

    def step(self, now: Optional[datetime] = None) -> list[SimLabel]:
        """Label up to ``per_tick`` Cases; returns what was labelled. Synchronous: it runs
        between pipeline events on the serving loop, like an API label request."""
        now = self.helix.clock.now() if now is None else now
        picked = self.candidates(now)
        if not picked:
            return []
        self._refresh()
        out: list[SimLabel] = []
        actor = self.cfg.actor
        for case in picked:
            truth = self.is_fraud_case(case)
            flip = self._rng.random() < self.cfg.error_rate
            fraud = truth != flip
            lab = Label.CONFIRMED_FRAUD if fraud else Label.FALSE_POSITIVE
            try:
                self.helix.open_case(case.case_id, actor)
                self.helix.label(case.case_id, actor, lab, NOTE)
            except Exception:  # noqa: BLE001 - e.g. a human claimed it meanwhile; skip it
                self.errors += 1
                log.warning("sim investigator: could not label %s", case.case_id, exc_info=True)
                continue
            self.labels_total += 1
            self.flipped += int(flip)
            if fraud:
                self.confirmed += 1
            else:
                self.false_positive += 1
            out.append(SimLabel(case.case_id, lab, truth, flip))
        return out

    # --- status ----------------------------------------------------------------------

    def status(self, enabled: bool = True) -> dict[str, Any]:
        return status_doc(self.cfg, enabled=enabled, labels_total=self.labels_total, confirmed=self.confirmed,
                          false_positive=self.false_positive, flipped=self.flipped, errors=self.errors)


def status_doc(cfg: Optional[SimInvestigatorConfig] = None, *, enabled: bool = False, labels_total: int = 0,
               confirmed: int = 0, false_positive: int = 0, flipped: int = 0, errors: int = 0) -> dict[str, Any]:
    """The ``sim_investigator`` block of GET /status (same keys when disabled)."""
    c = cfg if cfg is not None else SimInvestigatorConfig()
    return {"enabled": bool(enabled), "labels_total": int(labels_total), "confirmed": int(confirmed),
            "false_positive": int(false_positive), "error_rate": float(c.error_rate),
            "interval_s": float(c.interval_s), "per_tick": int(c.per_tick), "min_age_s": float(c.min_age_s),
            "flipped": int(flipped), "errors": int(errors), "actor": c.actor}


__all__ = ["ACTOR", "SimInvestigator", "SimInvestigatorConfig", "SimLabel", "status_doc"]
