"""Feedback_Learner: online Sensor_Weight adaptation from Feedback_Labels (design §16, Req 19).

On a label for a Case the learner reads the Case's Evidence_Chain. Every non-Canary
contributor whose undiscounted mass has ``f > l`` in at least one of its chain entries
moves by ``+lr`` (CONFIRMED_FRAUD) or ``-lr`` (FALSE_POSITIVE), clamped to
``[weight_lower, weight_upper]``. A contributor seen in several transactions of the chain
maps to one weight key and changes once. Each change is written as a ``WeightRecord`` to
Storage first, then applied to the live weight dict, then handed to the audit hook.

Weight keys:

* SENSOR contributors: the sensor name (``Helix.weights``, shared with stage-1 fusion);
* MOTIF entries: ``motif:<TYPE>`` (``MotifEngine.weights`` / ``CaseManager.motif_weights``,
  keyed by ``MotifType``);
* any other non-Canary type (ANTIBODY, ...): the contributor id in ``other_weights``,
  which the Immune_System reads for Antibody weights.

FUSED and CANARY contributors are never learned (Req 19.12).

Rollback to ``T`` (valid from the earliest WeightRecord, or startup when there is none, to
now) restores each contributor's latest recorded value at or before ``T`` or its initial
value, writing rollback-marked records. Labels and the set of applied Cases are kept.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Union

from helix.cases import Case, Label
from helix.config.schema import LearnConfig
from helix.explain import EntryKind, EvidenceChain, EvidenceEntry
from helix.explain.audit import AuditHook, AuditRecord, noop_audit_hook
from helix.motifs import MotifType

log = logging.getLogger(__name__)

WEIGHT_TABLE = "weight_history"
MOTIF_PREFIX = "motif:"
NOT_LEARNED = frozenset({"CANARY", "FUSED"})
DEFAULT_WEIGHT = 1.0


class WeightKind(str, Enum):
    SENSOR = "SENSOR"
    MOTIF = "MOTIF"
    OTHER = "OTHER"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class WeightRecord:  # insert-only
    contributor_id: str
    old: float
    new: float
    ts: datetime
    case_id: Optional[str]  # None for rollback records
    rollback_to: Optional[datetime] = None
    kind: WeightKind = WeightKind.SENSOR

    @property
    def is_rollback(self) -> bool:
        return self.rollback_to is not None

    def to_row(self) -> dict[str, Any]:
        return {"contributor_id": self.contributor_id, "kind": self.kind.value, "old": self.old,
                "new": self.new, "ts": self.ts, "case_id": self.case_id,
                "rollback": self.is_rollback, "rollback_to": self.rollback_to}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> WeightRecord:
        cid = str(row["contributor_id"])
        kind = row.get("kind") or (WeightKind.MOTIF if cid.startswith(MOTIF_PREFIX) else WeightKind.SENSOR)
        return cls(cid, float(row.get("old", row["new"])), float(row["new"]), row["ts"],
                   row.get("case_id"), row.get("rollback_to"), WeightKind(kind))


@dataclass(frozen=True, slots=True)
class LabelApplication:
    """One label submission seen by the learner; ``applied`` is False for repeats."""

    case_id: str
    label: Label
    ts: datetime
    applied: bool
    records: tuple[WeightRecord, ...]


class RollbackRangeError(ValueError):
    """Rollback time outside ``[earliest, latest]``; no weight was changed."""

    def __init__(self, t: datetime, earliest: datetime, latest: datetime) -> None:
        super().__init__(f"rollback time {t.isoformat()} is outside the valid range "
                         f"[{earliest.isoformat()}, {latest.isoformat()}]")
        self.t, self.earliest, self.latest = t, earliest, latest


ACTOR = "feedback_learner"
ROLLBACK_CASE = "~rollback"  # AuditRecord.case_id marker for rollback records


def weight_audit_record(rec: WeightRecord) -> AuditRecord:
    """The Audit_Ledger record for a weight change (Req 19.8): contributor id, old and new
    value, change time and the triggering Case id or a rollback marker."""
    rb = rec.rollback_to.isoformat() if rec.rollback_to is not None else None
    return AuditRecord(
        case_id=rec.case_id if rec.case_id is not None else ROLLBACK_CASE,
        ts=rec.ts,
        action="weight_rollback" if rec.is_rollback else "weight",
        actor=ACTOR,
        prev={"contributor_id": rec.contributor_id, "kind": rec.kind.value, "weight": rec.old},
        new={"contributor_id": rec.contributor_id, "kind": rec.kind.value, "weight": rec.new,
             "rollback_to": rb},
        note=None if rb is None else f"rollback to {rb}",
        evidence_digest=None,
    )


def _motif_type(key: str) -> Optional[MotifType]:
    if not key.startswith(MOTIF_PREFIX):
        return None
    try:
        return MotifType(key[len(MOTIF_PREFIX):])
    except ValueError:
        return None


def learnable(chain: Optional[EvidenceChain]) -> dict[str, tuple[WeightKind, float, bool]]:
    """``key -> (kind, chain weight, qualifies)`` for every non-Canary contributor key of the
    chain, in chain order. ``qualifies`` is True when any of its entries has undiscounted
    ``f > l``."""
    out: dict[str, tuple[WeightKind, float, bool]] = {}
    if chain is None:
        return out
    for e in chain.contributors():
        k = _key_of(e)
        if k is None:
            continue
        key, kind = k
        q = e.mass is not None and e.mass.f > e.mass.l
        prev = out.get(key)
        w = float(e.weight) if e.weight is not None else DEFAULT_WEIGHT
        out[key] = (kind, prev[1] if prev else w, q or (prev[2] if prev else False))
    return out


def _key_of(e: EvidenceEntry) -> Optional[tuple[str, WeightKind]]:
    ctype = None if e.ctype is None else str(e.ctype)
    if ctype in NOT_LEARNED:
        return None
    if e.kind is EntryKind.MOTIF:
        fid = e.detail.get("fuser_id")
        key = str(fid) if fid else e.contributor_id
        return (key, WeightKind.MOTIF) if _motif_type(key) is not None else (key, WeightKind.OTHER)
    if ctype == "SENSOR":
        return e.contributor_id, WeightKind.SENSOR
    if ctype == "MOTIF" and _motif_type(e.contributor_id) is not None:
        return e.contributor_id, WeightKind.MOTIF
    return e.contributor_id, WeightKind.OTHER


class FeedbackLearner:
    def __init__(
        self,
        cfg: Optional[LearnConfig] = None,
        clock: Any = None,
        storage: Any = None,
        audit_hook: AuditHook = noop_audit_hook,
        *,
        sensor_weights: Optional[MutableMapping[str, float]] = None,
        motif_weights: Sequence[MutableMapping[MotifType, float]] = (),
        other_weights: Optional[MutableMapping[str, float]] = None,
        default_weight: float = DEFAULT_WEIGHT,
    ) -> None:
        self.cfg = cfg if cfg is not None else LearnConfig()
        lo, hi, lr = self.cfg.weight_lower, self.cfg.weight_upper, self.cfg.learning_rate
        if not (0 <= lo < hi <= 1 and 0 < lr <= 0.5):
            raise ValueError(f"invalid learn config: bounds [{lo}, {hi}], learning_rate {lr}")
        self.lo, self.hi, self.lr = float(lo), float(hi), float(lr)
        if clock is None:
            from helix.sim.clock import ManualClock
            clock = ManualClock()
        self.clock = clock
        self.storage = storage
        self.audit_hook = audit_hook
        self.sensor_weights: MutableMapping[str, float] = sensor_weights if sensor_weights is not None else {}
        self.motif_weights: list[MutableMapping[MotifType, float]] = list(motif_weights)
        self.other_weights: MutableMapping[str, float] = other_weights if other_weights is not None else {}
        self.default_weight = float(default_weight)
        self.started_at: datetime = clock.now()

        self.history: list[WeightRecord] = []
        self.labels: list[LabelApplication] = []
        self.applied: set[str] = set()
        self.storage_errors = 0
        self._initial: dict[str, float] = {}
        self._kind: dict[str, WeightKind] = {}
        if storage is not None and hasattr(storage, "load_rows"):
            for row in storage.load_rows(WEIGHT_TABLE):
                if "contributor_id" in row and "new" in row and "ts" in row:
                    self._remember(WeightRecord.from_row(row))

    # --- weights --------------------------------------------------------------------

    def clamp(self, w: float) -> float:
        return min(self.hi, max(self.lo, float(w)))

    def kind_of(self, key: str) -> WeightKind:
        k = self._kind.get(key)
        if k is not None:
            return k
        if _motif_type(key) is not None:
            return WeightKind.MOTIF
        return WeightKind.OTHER if key in self.other_weights else WeightKind.SENSOR

    def _live(self, key: str, kind: WeightKind, fallback: Optional[float] = None) -> float:
        fb = self.default_weight if fallback is None else fallback
        if kind is WeightKind.MOTIF:
            mt = _motif_type(key)
            for m in self.motif_weights:
                if mt in m:
                    return float(m[mt])  # type: ignore[index]
            return fb
        store = self.sensor_weights if kind is WeightKind.SENSOR else self.other_weights
        return float(store.get(key, fb))

    def _set_live(self, key: str, kind: WeightKind, w: float) -> None:
        if kind is WeightKind.MOTIF:
            mt = _motif_type(key)
            for m in self.motif_weights:
                m[mt] = w  # type: ignore[index]
        elif kind is WeightKind.SENSOR:
            self.sensor_weights[key] = w
        else:
            self.other_weights[key] = w

    def weight(self, contributor_id: str) -> float:
        """Current Sensor_Weight for a weight key (sensor name, ``motif:<TYPE>``, antibody id)."""
        return self._live(contributor_id, self.kind_of(contributor_id))

    def initial(self, contributor_id: str) -> float:
        return self._initial.get(contributor_id, self.weight(contributor_id))

    def weight_history(self, contributor_id: str) -> list[WeightRecord]:
        return [r for r in self.history if r.contributor_id == contributor_id]

    def clamp_live(self, keys: Iterable[str]) -> None:
        """Bring the live weights of ``keys`` into the bounds (startup; no records)."""
        for key in keys:
            kind = self.kind_of(key)
            w = self._live(key, kind)
            if self.clamp(w) != w:
                self._set_live(key, kind, self.clamp(w))

    def restore_live(self) -> None:
        """Apply the latest recorded value of every recorded contributor (after a restart)."""
        latest: dict[str, WeightRecord] = {}
        for r in self.history:
            latest[r.contributor_id] = r
        for key, r in latest.items():
            self._set_live(key, r.kind, self.clamp(r.new))

    # --- labels ---------------------------------------------------------------------

    def on_label(self, case_id: str, label: Union[Label, str], chain: Optional[EvidenceChain],
                 now: Optional[datetime] = None) -> list[WeightRecord]:
        lab = Label(label)
        ts = self.clock.now() if now is None else now
        if case_id in self.applied:  # Req 19.11: at most one label per Case
            self.labels.append(LabelApplication(case_id, lab, ts, False, ()))
            return []
        self.applied.add(case_id)
        step = self.lr if lab is Label.CONFIRMED_FRAUD else -self.lr
        recs: list[WeightRecord] = []
        for key, (kind, chain_w, qualifies) in learnable(chain).items():
            if not qualifies:
                continue
            old = self._live(key, kind, chain_w)
            new = self.clamp(old + step)
            if new != old:
                recs.append(self._write(WeightRecord(key, old, new, ts, case_id, None, kind)))
        self.labels.append(LabelApplication(case_id, lab, ts, True, tuple(recs)))
        return recs

    # --- rollback -------------------------------------------------------------------

    def valid_range(self, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
        latest = self.clock.now() if now is None else now
        earliest = min(r.ts for r in self.history) if self.history else self.started_at
        return earliest, latest

    def rollback(self, t: datetime, now: Optional[datetime] = None) -> list[WeightRecord]:
        ts = self.clock.now() if now is None else now
        earliest, latest = self.valid_range(ts)
        if not (earliest <= t <= latest):
            raise RollbackRangeError(t, earliest, latest)
        target: dict[str, float] = {}
        for r in self.history:  # record order: the last one at or before t wins
            if r.ts <= t:
                target[r.contributor_id] = r.new
        recs: list[WeightRecord] = []
        for key, init in list(self._initial.items()):
            kind = self._kind[key]
            new = self.clamp(target.get(key, init))
            old = self._live(key, kind)
            if new != old:
                recs.append(self._write(WeightRecord(key, old, new, ts, None, t, kind)))
        return recs

    # --- internals ------------------------------------------------------------------

    def _remember(self, rec: WeightRecord) -> None:
        self._initial.setdefault(rec.contributor_id, rec.old)
        self._kind.setdefault(rec.contributor_id, rec.kind)
        self.history.append(rec)

    def _write(self, rec: WeightRecord) -> WeightRecord:
        if self.storage is not None:  # Storage first (Req 19.8), then the live dict
            try:
                self.storage.append(WEIGHT_TABLE, rec.to_row())
            except Exception:  # noqa: BLE001 - Storage problems must not stop learning (Req 24)
                self.storage_errors += 1
                log.exception("weight_history write failed for %s", rec.contributor_id)
        self._remember(rec)
        self._set_live(rec.contributor_id, rec.kind, rec.new)
        try:
            self.audit_hook(weight_audit_record(rec))
        except Exception:  # noqa: BLE001
            log.exception("weight audit hook failed for %s", rec.contributor_id)
        return rec


def attach(helix: Any, *, audit_hook: AuditHook = noop_audit_hook,
           other_weights: Optional[MutableMapping[str, float]] = None) -> FeedbackLearner:
    """Create a FeedbackLearner bound to ``helix``'s live weight dicts, register it in
    ``helix.feedback_hooks`` and expose it as ``helix.learner``. Idempotent."""
    existing = getattr(helix, "learner", None)
    if isinstance(existing, FeedbackLearner):
        return existing
    maps: list[MutableMapping[MotifType, float]] = [helix.motifs.weights]
    cm = getattr(helix.cases, "motif_weights", None)
    if cm is not None and cm is not helix.motifs.weights:
        maps.append(cm)
    learner = FeedbackLearner(helix.cfg.learn, helix.clock, helix.storage, audit_hook,
                              sensor_weights=helix.weights, motif_weights=maps, other_weights=other_weights)
    names = [str(s.name) for s in getattr(helix.stage1, "sensors", ())] + list(helix.weights)
    learner.clamp_live(dict.fromkeys(names + [MOTIF_PREFIX + t.value for t in MotifType]))
    if learner.history:
        learner.restore_live()

    def on_label(case: Case, label: Label) -> None:
        # Req 41.2: only a stored Feedback_Label teaches the learner. An AUTO_CLEARED closure
        # never reaches the feedback hooks; this guard keeps it that way if one ever does.
        if getattr(getattr(case, "closure_reason", None), "value", None) == "AUTO_CLEARED" or label is None:
            return
        learner.on_label(case.case_id, label, helix.chain(case.case_id))

    helix.feedback_hooks.append(on_label)
    helix.learner = learner
    return learner


__all__ = [
    "DEFAULT_WEIGHT", "ROLLBACK_CASE", "FeedbackLearner", "LabelApplication", "RollbackRangeError",
    "WeightKind", "WeightRecord", "attach", "learnable", "weight_audit_record",
]
