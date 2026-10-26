"""Immune_System: clonal selection, negative selection and a bounded Antibody_Pool (design §18,
Requirement 21).

Lifecycle of an Antibody::

    trigger (miss / low-risk CONFIRMED_FRAUD Case)
      -> N clones of the signature, each parameter jittered by at most ±mutation_rate
      -> HELD        while |Self_Set| < self_set_min           (Req 21.5)
           -> DISCARDED "expired" after hold_expiry_s            (Req 21.6)
      -> self-match rate on the Self_Set
           -> rate <  limit: ACTIVE                              (Req 21.3)
           -> rate >= limit: DISCARDED "self_match" + rate       (Req 21.4)
      ACTIVE -> RETIRED "capacity"             (pool full; lowest affinity, earliest created)
             -> RETIRED "false_positive_limit" (FP labels > fp_limit)

Every Antibody record is kept after it leaves the pool (lineage, Req 21.12).

Antibody = axis-aligned box in feature space: ``center`` and ``half_width`` over the
dimensions in ``mask`` (the signature's non-missing dimensions). A feature vector matches when
every masked dimension is present and ``|f - c| <= h``; a NaN there is no match (Req 7.5). A
match emits ``Mass(mass_f, 0, 1 - mass_f)`` as contributor ``antibody:<id>`` with weight
``weights.get(contributor_id, 1.0)`` (the Feedback_Learner's ``other_weights``).

Self_Set: Algorithm-R reservoir (capacity ``self_set_capacity``) of ``(tx_id, f)`` over every
observed event that references no Canary. Events of a CONFIRMED_FRAUD Case are purged when the
label arrives (Req 21.7), and so is a recorded miss (a known attack event is not "self").

No ground truth: the system learns of attack events only through ``on_miss(tx_id)``, which
``helix.immune.miss_detector`` calls (Req 21.15). Nothing here imports ``helix.evaluation``.

Determinism: clones come from ``numpy.random.default_rng(seed)``, the reservoir from
``random.Random(seed)``. ``to_dict`` / ``from_dict`` round-trip the whole state, rng states
included, so a restored system evaluates and evolves identically.
"""

from __future__ import annotations

import logging
import math
import random
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional, Sequence

import numpy as np

from helix.config.schema import ImmuneConfig
from helix.fusion import Contribution, ContributorType, Mass

from .features import D, DIMS, UNATTRIBUTED, technique_tag, technique_tags

log = logging.getLogger(__name__)

CONTRIBUTOR_PREFIX = "antibody:"
FORMAT = "helix.immune/1"


class Status(str, Enum):
    HELD = "HELD"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    DISCARDED = "DISCARDED"

    def __str__(self) -> str:
        return self.value


class Reason(str, Enum):
    SELF_MATCH = "self_match"  # discard: self-match rate >= limit
    EXPIRED = "expired"  # discard: held longer than hold_expiry_s
    CAPACITY = "capacity"  # retire: pool full, lowest affinity
    FP_LIMIT = "false_positive_limit"  # retire: FP labels > fp_limit

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class AntibodyLineage:
    """Read-only lineage view (design data model; Req 21.12, 21.13). Kept after retirement."""

    origin: str  # "miss:<event_id>" | "case:<case_id>"
    parent_id: Optional[str]
    created_at: datetime
    tags: tuple[str, ...]
    retired_at: Optional[datetime] = None
    retire_reason: Optional[str] = None  # "pool_capacity" | "false_positive_limit"


@dataclass(frozen=True, slots=True)
class AntibodyStats:
    """Read-only statistics view (design data model)."""

    cf_matches: tuple[datetime, ...]  # CONFIRMED_FRAUD label times within the affinity window
    fp_matches: tuple[datetime, ...]
    cf_count: int
    fp_count: int
    total_matches: int
    self_match_rate: Optional[float]


#: Design data-model names for the retirement reasons.
_RETIRE_NAME = {"capacity": "pool_capacity", "false_positive_limit": "false_positive_limit"}


@dataclass
class Antibody:
    ab_id: str
    seq: int  # creation order (ties on created_at)
    center: np.ndarray  # (D,), 0 outside mask
    half_width: np.ndarray  # (D,), 0 outside mask
    mask: np.ndarray  # (D,) bool: the dimensions this antibody matches on
    mass_f: float
    parent_id: Optional[str]
    origin: str  # "miss:<tx_id>" | "case:<case_id>"
    created_at: datetime
    tags: tuple[str, ...]
    status: Status = Status.HELD
    self_match_rate: Optional[float] = None
    activated_at: Optional[datetime] = None
    matches: int = 0  # feature-vector matches while active
    last_match_at: Optional[datetime] = None
    cf_matches: deque = field(default_factory=deque)  # label times of CONFIRMED_FRAUD Cases it matched
    fp_matches: deque = field(default_factory=deque)  # label times of FALSE_POSITIVE Cases it matched
    cf_count: int = 0
    fp_count: int = 0
    ended: Optional[tuple[datetime, Reason]] = None  # retirement or discard (time, reason)

    @property
    def contributor_id(self) -> str:
        return CONTRIBUTOR_PREFIX + self.ab_id

    @property
    def active(self) -> bool:
        return self.status is Status.ACTIVE

    @property
    def retired(self) -> Optional[tuple[datetime, Reason]]:
        return self.ended if self.status is Status.RETIRED else None

    @property
    def mass(self) -> Mass:
        return Mass(self.mass_f, 0.0, 1.0 - self.mass_f)

    @property
    def half_widths(self) -> np.ndarray:
        return self.half_width

    @property
    def lineage(self) -> AntibodyLineage:
        r = self.retired
        return AntibodyLineage(self.origin, self.parent_id, self.created_at, self.tags,
                               None if r is None else r[0], None if r is None else _RETIRE_NAME[r[1].value])

    @property
    def stats(self) -> AntibodyStats:
        return AntibodyStats(tuple(self.cf_matches), tuple(self.fp_matches), self.cf_count, self.fp_count,
                             self.matches, self.self_match_rate)

    def dims(self) -> tuple[str, ...]:
        return tuple(n for n, m in zip(DIMS, self.mask) if m)

    def matches_vector(self, fv: np.ndarray) -> bool:
        return bool(_match_rows(np.asarray(fv, dtype=float)[None, :], self.center, self.half_width, self.mask)[0])

    def lineage_record(self) -> dict[str, Any]:
        """Req 21.12: origin, parent, creation, matches, retirement (time and reason)."""
        end = self.ended
        return {
            "ab_id": self.ab_id, "contributor_id": self.contributor_id, "status": self.status.value,
            "origin": self.origin, "parent_id": self.parent_id, "created_at": self.created_at,
            "activated_at": self.activated_at, "tags": list(self.tags), "dims": list(self.dims()),
            "self_match_rate": self.self_match_rate, "matches": self.matches, "last_match_at": self.last_match_at,
            "confirmed_fraud_matches": self.cf_count, "false_positive_matches": self.fp_count,
            "ended_at": None if end is None else end[0], "end_reason": None if end is None else end[1].value,
            "retired_at": end[0] if end is not None and self.status is Status.RETIRED else None,
            "retired_reason": end[1].value if end is not None and self.status is Status.RETIRED else None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ab_id": self.ab_id, "seq": self.seq, "center": self.center.tolist(),
            "half_width": self.half_width.tolist(), "mask": [bool(x) for x in self.mask], "mass_f": self.mass_f,
            "parent_id": self.parent_id, "origin": self.origin, "created_at": self.created_at.isoformat(),
            "tags": list(self.tags), "status": self.status.value, "self_match_rate": self.self_match_rate,
            "activated_at": None if self.activated_at is None else self.activated_at.isoformat(),
            "matches": self.matches,
            "last_match_at": None if self.last_match_at is None else self.last_match_at.isoformat(),
            "cf_matches": [t.isoformat() for t in self.cf_matches],
            "fp_matches": [t.isoformat() for t in self.fp_matches],
            "cf_count": self.cf_count, "fp_count": self.fp_count,
            "ended": None if self.ended is None else [self.ended[0].isoformat(), self.ended[1].value],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Antibody:
        def ts(v: Any) -> Optional[datetime]:
            return None if v is None else datetime.fromisoformat(v)

        end = d.get("ended")
        return cls(
            ab_id=d["ab_id"], seq=int(d["seq"]), center=np.asarray(d["center"], dtype=float),
            half_width=np.asarray(d["half_width"], dtype=float), mask=np.asarray(d["mask"], dtype=bool),
            mass_f=float(d["mass_f"]), parent_id=d.get("parent_id"), origin=d["origin"],
            created_at=datetime.fromisoformat(d["created_at"]), tags=tuple(d.get("tags", ())),
            status=Status(d["status"]), self_match_rate=d.get("self_match_rate"),
            activated_at=ts(d.get("activated_at")), matches=int(d.get("matches", 0)),
            last_match_at=ts(d.get("last_match_at")),
            cf_matches=deque(datetime.fromisoformat(t) for t in d.get("cf_matches", ())),
            fp_matches=deque(datetime.fromisoformat(t) for t in d.get("fp_matches", ())),
            cf_count=int(d.get("cf_count", 0)), fp_count=int(d.get("fp_count", 0)),
            ended=None if end is None else (datetime.fromisoformat(end[0]), Reason(end[1])),
        )


@dataclass(frozen=True, slots=True)
class AntibodyMatch:
    ab_id: str
    contributor_id: str
    mass: Mass
    weight: float
    dims: tuple[str, ...]

    @property
    def contribution(self) -> Contribution:
        return Contribution(self.contributor_id, ContributorType.ANTIBODY, self.mass, self.weight)


@dataclass(frozen=True, slots=True)
class DiscardRecord:
    """Req 21.4 / 21.6: candidate id, self-match rate (None for "expired") and the reason."""

    ab_id: str
    reason: Reason
    self_match_rate: Optional[float]
    at: datetime


@dataclass(frozen=True, slots=True)
class TriggerRecord:
    """One clonal-selection trigger: a miss or a low-risk CONFIRMED_FRAUD Case."""

    origin: str
    at: datetime
    candidates: tuple[str, ...]
    note: Optional[str] = None  # e.g. "no_signature" when the event's features were not cached


def _match_rows(X: np.ndarray, c: np.ndarray, h: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Rows of ``X`` inside the box on the masked dimensions (NaN there: no match)."""
    with np.errstate(invalid="ignore"):
        inside = np.abs(X - c) <= h  # NaN compares False
    return np.all(inside | ~m, axis=-1)


def _w(v: Any) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 1.0
    return min(1.0, max(0.0, x)) if math.isfinite(x) else 1.0


class ImmuneSystem:
    def __init__(
        self,
        cfg: Optional[ImmuneConfig] = None,
        clock: Any = None,
        seed: int = 0,
        *,
        weights: Optional[MutableMapping[str, float]] = None,
        is_canary: Optional[Callable[[str], bool]] = None,
        tagger: Optional[Callable[[str], Iterable[str]]] = None,
        feature_cache_size: int = 50_000,
    ) -> None:
        self.cfg = cfg if cfg is not None else ImmuneConfig()
        if clock is None:
            from helix.sim.clock import ManualClock
            clock = ManualClock()
        self.clock = clock
        self.seed = int(seed)
        #: Antibody Sensor_Weights keyed by contributor id (``FeedbackLearner.other_weights``).
        self.weights: MutableMapping[str, float] = weights if weights is not None else {}
        #: Canary hook (Req 21.7, 33): True for a Canary account id. Default: no Canaries.
        self.is_canary = is_canary
        #: Technique hook for miss-origin antibodies: tx_id -> technique kinds (e.g. genome type).
        self.tagger = tagger
        self.feature_cache_size = int(feature_cache_size)
        self.self_capacity = max(int(self.cfg.self_set_capacity), int(self.cfg.self_set_min))

        self._rng = np.random.default_rng(self.seed)
        self._res_rng = random.Random(self.seed)
        self._seq = 0
        self.antibodies: dict[str, Antibody] = {}  # every antibody ever created (lineage)
        self._active: list[str] = []  # activation order
        self._held: list[str] = []
        self.discards: list[DiscardRecord] = []
        self.triggers: list[TriggerRecord] = []
        self.retirements: list[tuple[str, datetime, Reason]] = []
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()  # tx_id -> f (recent events)
        self._self_ids: list[str] = []
        self._self_X = np.empty((self.self_capacity, D))
        self._self_slot: dict[str, int] = {}
        self._self_seen = 0
        self._purged: set[str] = set()  # never (re-)admitted to the Self_Set
        self._pool_cache: Optional[tuple[list[Antibody], np.ndarray, np.ndarray, np.ndarray]] = None

    # ------------------------------------------------------------------ queries

    @property
    def self_size(self) -> int:
        return len(self._self_ids)

    def self_ids(self) -> list[str]:
        return list(self._self_ids)

    def pool(self) -> list[Antibody]:
        """Active antibodies in activation order."""
        return [self.antibodies[i] for i in self._active]

    def candidates(self) -> list[Antibody]:
        """Held (inactive) candidates in creation order."""
        return [self.antibodies[i] for i in self._held]

    def lineage(self) -> list[dict[str, Any]]:
        """Lineage records of every antibody ever created, retired and discarded ones included."""
        return [a.lineage_record() for a in sorted(self.antibodies.values(), key=lambda a: a.seq)]

    def features_of(self, tx_id: str) -> Optional[np.ndarray]:
        f = self._cache.get(tx_id)
        return None if f is None else f.copy()

    def affinity(self, ab: Antibody, now: Optional[datetime] = None) -> int:
        """CONFIRMED_FRAUD matches minus FALSE_POSITIVE matches within the affinity window."""
        now = self.clock.now() if now is None else now
        lo = now - timedelta(seconds=float(self.cfg.affinity_window_s))
        cf = sum(1 for t in ab.cf_matches if lo <= t <= now)
        fp = sum(1 for t in ab.fp_matches if lo <= t <= now)
        return cf - fp

    def summary(self) -> dict[str, Any]:
        by: dict[str, int] = {s.value: 0 for s in Status}
        for a in self.antibodies.values():
            by[a.status.value] += 1
        return {"active": len(self._active), "held": len(self._held), "pool_max": int(self.cfg.pool_max),
                "self_set_size": self.self_size, "self_set_min": int(self.cfg.self_set_min),
                "self_set_capacity": self.self_capacity, "by_status": by, "triggers": len(self.triggers),
                "discards": len(self.discards), "retirements": len(self.retirements)}

    # ------------------------------------------------------------------ PERCEIVE

    def _pool_arrays(self) -> tuple[list[Antibody], np.ndarray, np.ndarray, np.ndarray]:
        if self._pool_cache is None:
            abs_ = self.pool()
            if abs_:
                C = np.stack([a.center for a in abs_])
                H = np.stack([a.half_width for a in abs_])
                M = np.stack([a.mask for a in abs_])
            else:
                C = H = np.empty((0, D))
                M = np.empty((0, D), dtype=bool)
            self._pool_cache = (abs_, C, H, M)
        return self._pool_cache

    def evaluate(self, fv: np.ndarray, *, record: bool = True) -> list[AntibodyMatch]:
        """Active antibodies matching ``fv`` (vectorised over the pool). No ground truth."""
        abs_, C, H, M = self._pool_arrays()
        if not abs_:
            return []
        x = np.asarray(fv, dtype=float)
        with np.errstate(invalid="ignore"):
            hit = np.all((np.abs(x[None, :] - C) <= H) | ~M, axis=1)
        out: list[AntibodyMatch] = []
        now = self.clock.now() if record else None
        for k in np.flatnonzero(hit):
            a = abs_[int(k)]
            if record:
                a.matches += 1
                a.last_match_at = now
            out.append(AntibodyMatch(a.ab_id, a.contributor_id, a.mass, _w(self.weights.get(a.contributor_id, 1.0)),
                                     a.dims()))
        return out

    def contributions(self, fv: np.ndarray, *, record: bool = False) -> list[Contribution]:
        """ANTIBODY contributions for the Evidence_Fuser (Req 21.8)."""
        return [m.contribution for m in self.evaluate(fv, record=record)]

    # ------------------------------------------------------------------ REMEMBER

    def observe(self, tx_id: str, fv: np.ndarray, ev: Any = None, now: Optional[datetime] = None) -> None:
        """Cache the event's features and offer it to the Self_Set (Canary events excluded)."""
        cache = self._cache
        cache[tx_id] = np.asarray(fv, dtype=float)
        cache.move_to_end(tx_id)
        while len(cache) > self.feature_cache_size:
            cache.popitem(last=False)
        if not self._references_canary(ev) and tx_id not in self._purged:
            self._self_add(tx_id, cache[tx_id])
        self.tick(now)

    def _references_canary(self, ev: Any) -> bool:
        f = self.is_canary
        if f is None or ev is None:
            return False
        dev = getattr(ev, "device_id", None)
        return bool(f(getattr(ev, "src", "")) or f(getattr(ev, "dst", ""))
                    or (isinstance(dev, str) and f(dev)))

    def _self_add(self, tx_id: str, fv: np.ndarray) -> None:
        if tx_id in self._self_slot:
            return
        self._self_seen += 1
        n = len(self._self_ids)
        if n < self.self_capacity:
            self._self_slot[tx_id] = n
            self._self_ids.append(tx_id)
            self._self_X[n] = fv
            return
        j = self._res_rng.randrange(self._self_seen)
        if j < self.self_capacity:
            old = self._self_ids[j]
            del self._self_slot[old]
            self._self_ids[j] = tx_id
            self._self_slot[tx_id] = j
            self._self_X[j] = fv

    def purge_self(self, tx_ids: Iterable[str]) -> int:
        """Remove events from the Self_Set and never re-admit them. Returns the number removed."""
        n = 0
        for tx in tx_ids:
            self._purged.add(tx)
            j = self._self_slot.pop(tx, None)
            if j is None:
                continue
            last = len(self._self_ids) - 1
            if j != last:
                moved = self._self_ids[last]
                self._self_ids[j] = moved
                self._self_X[j] = self._self_X[last]
                self._self_slot[moved] = j
            self._self_ids.pop()
            n += 1
        return n

    def self_match_rate(self, ab: Antibody) -> float:
        n = len(self._self_ids)
        if n == 0:
            return 0.0
        hits = _match_rows(self._self_X[:n], ab.center, ab.half_width, ab.mask)
        return float(np.count_nonzero(hits)) / n

    # ------------------------------------------------------------------ LEARN: triggers

    def on_miss(self, tx_id: str) -> list[Antibody]:
        """A recorded miss (called by the MissDetector only, with nothing but the event id):
        purge the event from the Self_Set and clone its cached feature signature.

        The candidates are *held*; negative selection runs on the next ``tick`` (every
        ``observe`` ticks, and the API's miss task ticks after each batch). Deferring it lets
        a batch of misses from one campaign purge all of its events from the Self_Set before
        any clone is tested against it, so sibling attack events never count as "self"."""
        now = self.clock.now()
        origin = f"miss:{tx_id}"
        self.purge_self([tx_id])
        fv = self._cache.get(tx_id)
        if fv is None or not (~np.isnan(fv)).any():
            self.triggers.append(TriggerRecord(origin, now, (), "no_signature"))
            log.info("immune: miss %s has no cached features; no candidates", tx_id)
            return []
        mask = ~np.isnan(fv)
        center = np.where(mask, fv, 0.0)
        parent = next((m.ab_id for m in self.evaluate(fv, record=False)), None)
        return self._clone(origin, center, self._base_width(center, mask), mask, parent, self._miss_tags(tx_id), now)

    def on_low_risk_confirmed(self, case: Any) -> list[Antibody]:
        """A CONFIRMED_FRAUD Case whose Risk_Score at creation was below the alerting threshold:
        clone the bounding box of its cached Transaction_Event vectors (dimensions present in
        every event). Selection runs immediately (the Case's events were purged first)."""
        now = self.clock.now()
        origin = f"case:{case.case_id}"
        vs = [self._cache[tx] for tx in sorted(case.tx_ids) if tx in self._cache]
        X = np.stack(vs) if vs else np.empty((0, D))
        mask = ~np.isnan(X).any(axis=0) if vs else np.zeros(D, dtype=bool)
        if not mask.any():
            self.triggers.append(TriggerRecord(origin, now, (), "no_signature"))
            return []
        Z = np.where(mask, X, 0.0)
        lo, hi = Z.min(axis=0), Z.max(axis=0)
        center = np.where(mask, (lo + hi) / 2.0, 0.0)
        h0 = self._base_width(center, mask, spread=np.where(mask, (hi - lo) / 2.0, 0.0))
        parent = None
        for v in vs:
            m = self.evaluate(v, record=False)
            if m:
                parent = m[0].ab_id
                break
        out = self._clone(origin, center, h0, mask, parent, self._case_tags(case), now)
        self.tick(now)
        return out

    on_confirmed_low_risk_case = on_low_risk_confirmed  # design §18 name

    def _base_width(self, center: np.ndarray, mask: np.ndarray, spread: Optional[np.ndarray] = None) -> np.ndarray:
        """Signature half-width ``h0`` per dimension: ``(max(w, spread) + r·c) / (1 - r)``.

        With ``c' = clip(c·(1 + u_c))`` and ``h = h0·(1 + u_h)``, ``|u| <= r``, this gives
        ``h >= max(w, spread) + r·c >= |c' - c| + spread``, so every clone still contains the
        origin signature (the missed event's vector, or every event of the origin Case's
        bounding box). Without the ``r·c`` term a ±10% centre jitter on a value near 1
        (e.g. ``counterparty_unknown``) would move the box off the very event it was cloned
        from. ``mutation_rate`` is capped at 0.5 by the config, so ``1 - r >= 0.5``."""
        r = float(self.cfg.mutation_rate)
        w = np.full(D, float(self.cfg.antibody_half_width))
        if spread is not None:
            w = np.maximum(w, spread)
        return np.where(mask, (w + r * np.abs(center)) / max(1.0 - r, 1e-9), 0.0)

    def _miss_tags(self, tx_id: str) -> tuple[str, ...]:
        """Req 21.13: technique ids of the origin campaign's Pattern_Genome type, via the
        ``tagger`` hook (tx_id -> kinds). The hook is metadata only: it never changes
        whether or how an antibody matches."""
        kinds: list[Any] = []
        if self.tagger is not None:
            try:
                kinds = list(self.tagger(tx_id) or ())
            except Exception:  # noqa: BLE001 - tags are metadata; never fail a trigger
                log.exception("immune: tagger failed for %s", tx_id)
        return technique_tags(kinds) or (UNATTRIBUTED,)

    @staticmethod
    def _case_tags(case: Any) -> tuple[str, ...]:
        """Req 21.13: the origin Case's HELIX_Technique tags (``technique_tags`` /
        ``techniques`` when the Case carries them), else the techniques of its Motif types."""
        for attr in ("technique_tags", "techniques"):
            v = getattr(case, attr, None)
            if v:
                tags = technique_tags(getattr(t, "technique_id", t) for t in v)
                if tags:
                    return tags
        motifs = getattr(case, "motifs", None) or {}
        return technique_tags(getattr(m, "motif_type", None) for m in motifs.values()) or (UNATTRIBUTED,)

    def _clone(self, origin: str, center: np.ndarray, h0: np.ndarray, mask: np.ndarray,
               parent: Optional[str], tags: tuple[str, ...], now: datetime) -> list[Antibody]:
        """Clonal selection: ``candidates_per_trigger`` clones, each centre and half-width
        parameter mutated by a factor ``1 + U(-r, r)``. Clones are created HELD."""
        r = float(self.cfg.mutation_rate)
        n = int(self.cfg.candidates_per_trigger)
        out: list[Antibody] = []
        for _ in range(n):
            u_c = self._rng.uniform(-r, r, D)
            u_h = self._rng.uniform(-r, r, D)
            c = np.where(mask, np.clip(center * (1.0 + u_c), 0.0, 1.0), 0.0)
            h = np.where(mask, h0 * (1.0 + u_h), 0.0)
            self._seq += 1
            ab = Antibody(f"AB{self._seq:06d}", self._seq, c, h, mask.copy(), float(self.cfg.antibody_mass),
                          parent, origin, now, tags)
            self.antibodies[ab.ab_id] = ab
            self._held.append(ab.ab_id)
            out.append(ab)
        self.triggers.append(TriggerRecord(origin, now, tuple(a.ab_id for a in out)))
        return out

    # ------------------------------------------------------------------ negative selection

    def tick(self, now: Optional[datetime] = None) -> None:
        """Expire held candidates (Req 21.6), then evaluate the rest once the Self_Set has
        reached the minimum (Req 21.3-21.5)."""
        if not self._held:
            return
        now = self.clock.now() if now is None else now
        expiry = timedelta(seconds=float(self.cfg.hold_expiry_s))
        keep: list[str] = []
        for i in self._held:
            a = self.antibodies[i]
            if now - a.created_at > expiry:
                self._end(a, Status.DISCARDED, Reason.EXPIRED, now)
                self.discards.append(DiscardRecord(a.ab_id, Reason.EXPIRED, None, now))
            else:
                keep.append(i)
        self._held = keep
        if len(self._self_ids) < int(self.cfg.self_set_min):
            return
        held, self._held = self._held, []
        for i in held:
            self.select(self.antibodies[i], now)

    def select(self, ab: Antibody, now: Optional[datetime] = None) -> Status:
        """Negative selection of one candidate against the current Self_Set."""
        now = self.clock.now() if now is None else now
        if len(self._self_ids) < int(self.cfg.self_set_min):
            if ab.ab_id not in self._held:
                self._held.append(ab.ab_id)
            return ab.status
        rate = self.self_match_rate(ab)
        ab.self_match_rate = rate
        if rate < float(self.cfg.self_match_limit):
            self._activate(ab, now)
        else:
            self._end(ab, Status.DISCARDED, Reason.SELF_MATCH, now)
            self.discards.append(DiscardRecord(ab.ab_id, Reason.SELF_MATCH, rate, now))
        return ab.status

    def _activate(self, ab: Antibody, now: datetime) -> None:
        while len(self._active) >= int(self.cfg.pool_max):
            victim = self.capacity_victim(now)
            self.retire(victim, Reason.CAPACITY, now)
        ab.status = Status.ACTIVE
        ab.activated_at = now
        self._active.append(ab.ab_id)
        self._pool_cache = None

    def capacity_victim(self, now: Optional[datetime] = None) -> Antibody:
        """Lowest affinity, earliest created among ties (Req 21.10)."""
        now = self.clock.now() if now is None else now
        return min(self.pool(), key=lambda a: (self.affinity(a, now), a.created_at, a.seq))

    def retire(self, ab: Antibody, reason: Reason, now: Optional[datetime] = None) -> None:
        now = self.clock.now() if now is None else now
        if ab.status is not Status.ACTIVE:
            return
        self._active.remove(ab.ab_id)
        self._pool_cache = None
        self._end(ab, Status.RETIRED, reason, now)
        self.retirements.append((ab.ab_id, now, reason))
        log.info("immune: retired %s (%s)", ab.ab_id, reason.value)

    def _end(self, ab: Antibody, status: Status, reason: Reason, now: datetime) -> None:
        ab.status = status
        ab.ended = (now, reason)

    # ------------------------------------------------------------------ LEARN: labels

    def on_label(self, case: Any, label: Any, *, alert_threshold: Optional[float] = None) -> list[Antibody]:
        """Feedback hook. Credits / debits every antibody that contributed to the Case's
        events; CONFIRMED_FRAUD purges the Case's events from the Self_Set and, for a Case
        created below the alerting threshold, triggers clonal selection (Req 21.2)."""
        now = self.clock.now()
        lab = str(getattr(label, "value", label))
        ids = sorted({c.contributor_id[len(CONTRIBUTOR_PREFIX):]
                      for fa in getattr(case, "assessments", {}).values() for c in fa.contributions
                      if str(getattr(c.kind, "value", c.kind)) == ContributorType.ANTIBODY.value
                      and c.contributor_id.startswith(CONTRIBUTOR_PREFIX)})
        for i in ids:
            a = self.antibodies.get(i)
            if a is None:
                continue
            if lab == "CONFIRMED_FRAUD":
                a.cf_count += 1
                a.cf_matches.append(now)
            elif lab == "FALSE_POSITIVE":
                a.fp_count += 1
                a.fp_matches.append(now)
                if a.fp_count > int(self.cfg.fp_limit):
                    self.retire(a, Reason.FP_LIMIT, now)
            self._prune(a, now)
        if lab != "CONFIRMED_FRAUD":
            return []
        self.purge_self(case.tx_ids)
        thr = alert_threshold
        if thr is not None and float(case.risk_at_creation) < float(thr):
            return self.on_low_risk_confirmed(case)
        return []

    def _prune(self, a: Antibody, now: datetime) -> None:
        lo = now - timedelta(seconds=float(self.cfg.affinity_window_s))
        for d in (a.cf_matches, a.fp_matches):
            while d and d[0] < lo:
                d.popleft()

    # ------------------------------------------------------------------ snapshots

    def to_dict(self, *, include_cache: bool = True) -> dict[str, Any]:
        """Full state (pool, candidates, lineage, Self_Set, rng states). Restoring it is what
        makes a replay that includes antibody contributions exact."""
        n = len(self._self_ids)
        st = self._res_rng.getstate()
        return {
            "format": FORMAT, "seed": self.seed, "seq": self._seq,
            "rng": self._rng.bit_generator.state, "res_rng": [st[0], list(st[1]), st[2]],
            "antibodies": [a.to_dict() for a in sorted(self.antibodies.values(), key=lambda a: a.seq)],
            "active": list(self._active), "held": list(self._held),
            "discards": [[d.ab_id, d.reason.value, d.self_match_rate, d.at.isoformat()] for d in self.discards],
            "triggers": [[t.origin, t.at.isoformat(), list(t.candidates), t.note] for t in self.triggers],
            "retirements": [[i, t.isoformat(), r.value] for i, t, r in self.retirements],
            "self_ids": list(self._self_ids), "self_X": self._self_X[:n].tolist(), "self_seen": self._self_seen,
            "purged": sorted(self._purged),
            "cache": [[k, v.tolist()] for k, v in self._cache.items()] if include_cache else [],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], cfg: Optional[ImmuneConfig] = None, clock: Any = None, **kw: Any
                  ) -> ImmuneSystem:
        if d.get("format") != FORMAT:
            raise ValueError(f"not an immune snapshot: format {d.get('format')!r}")
        s = cls(cfg, clock, int(d["seed"]), **kw)
        s._seq = int(d["seq"])
        s._rng.bit_generator.state = d["rng"]
        rs = d["res_rng"]
        s._res_rng.setstate((rs[0], tuple(rs[1]), rs[2]))
        s.antibodies = {a["ab_id"]: Antibody.from_dict(a) for a in d["antibodies"]}
        s._active = list(d["active"])
        s._held = list(d["held"])
        s.discards = [DiscardRecord(i, Reason(r), rate, datetime.fromisoformat(t)) for i, r, rate, t in d["discards"]]
        s.triggers = [TriggerRecord(o, datetime.fromisoformat(t), tuple(c), note) for o, t, c, note in d["triggers"]]
        s.retirements = [(i, datetime.fromisoformat(t), Reason(r)) for i, t, r in d["retirements"]]
        ids = list(d["self_ids"])
        if len(ids) > s.self_capacity:
            raise ValueError("immune snapshot: Self_Set larger than the configured capacity")
        s._self_ids = ids
        s._self_slot = {tx: j for j, tx in enumerate(ids)}
        if ids:
            s._self_X[: len(ids)] = np.asarray(d["self_X"], dtype=float)
        s._self_seen = int(d["self_seen"])
        s._purged = set(d.get("purged", ()))
        s._cache = OrderedDict((k, np.asarray(v, dtype=float)) for k, v in d.get("cache", ()))
        return s


def antibody_ids(contributions: Sequence[Contribution]) -> list[str]:
    return [c.contributor_id[len(CONTRIBUTOR_PREFIX):] for c in contributions
            if c.kind is ContributorType.ANTIBODY and c.contributor_id.startswith(CONTRIBUTOR_PREFIX)]


__all__ = [
    "CONTRIBUTOR_PREFIX", "Antibody", "AntibodyMatch", "DiscardRecord", "ImmuneSystem", "Reason", "Status",
    "TriggerRecord", "antibody_ids",
]
