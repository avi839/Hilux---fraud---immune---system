"""Deception layer: Canary_Accounts, Canary_Payees and Canary_Devices (Req 33).

Design decisions:
- Deterministic generation (Req 33.1): ``CanaryRegistry.generate(seed, counts)`` draws the ids
  with ``random.Random`` seeded from ``seed`` (salted), default 10 of each kind. The same seed
  and counts always give the same ids; the three kinds are disjoint.
- Indistinguishable ids: Canaries use exactly the Synthetic_Generator formats
  (``helix.sim.generator``): accounts ``ACC`` + 6 digits, payees ``EXT`` + 6 digits (the
  generator's external-payee form), devices ``DEV`` + 6 digits + ``-`` + 0..2. The numeric part
  is drawn from ``[CANARY_ID_FLOOR, 999999]``; the generator numbers accounts, payees and
  devices by account index, which is below ``GENERATOR_MAX_ACCOUNTS`` (= 100 000, the upper
  bound of ``generator.accounts``), so no legitimate generator id is ever a Canary (Req 33.5).
  The Attacker_Agent's minted ids use 9 digits (``EXT``/``DEV`` + 9), so they cannot collide
  either. ``reserved`` lets a caller exclude further ids (e.g. a loaded dataset's accounts).
  Limitation: with a small generator population (say 1 000 accounts) a Canary number is larger
  than every real one; the *format* is identical but the magnitude is not hidden.
- Reference detection (Req 33.2, 33.3): ``references(ev)`` checks the account, payee /
  destination and device fields of Transaction_Events (``src``, ``dst``, ``device_id``) and
  Auth_Events (``account_id``, ``payee_account_id``, ``device_id``); events are duck-typed.
  A Canary of any kind counts in any of these fields.
- Evidence (Req 33.2): ``contribution(ev)`` returns one ``Contribution`` with contributor id
  ``"canary"`` (``CANARY_CONTRIBUTOR_ID``), kind ``CANARY`` and mass
  ``Mass(f, 0, 1 - f)`` with ``f = fraud_mass`` (default 0.99, configurable in [0.95, 1]) and
  a fixed weight of 1.0 that never comes from a learned weight table.
  Not learnable: ``helix.learn.learner`` already skips entries whose ctype is ``CANARY``
  (``NOT_LEARNED``). Until ``ContributorType.CANARY`` exists, the kind is a local str-enum whose
  ``str()`` is ``"CANARY"`` (``fuse`` only uses ``str(kind)``); once the enum gains the member it
  is picked up automatically. Learners and weight tables can also skip any contributor id
  for which ``is_canary_contributor`` is true (``"canary"`` or a ``"canary:"`` prefix).
- Hits: ``observe(ev)`` records each (event, field, Canary) reference with the event time;
  ``hits()`` lists them and ``first_reference(id)`` gives the first reference time. Hit storage
  is bounded (``max_hits``); first-reference times are kept for every touched Canary.
- Campaign report (Req 33.8): ``campaign_report(campaign_of)`` groups hits by Attack_Campaign.
  ``campaign_of`` maps an event id to a campaign id (or None) and is supplied by the
  Evaluation_Monitor side, so this module never reads attack labels itself.
- Attacker population (Req 33.7): ``population(base_ids, rng)`` returns ``base_ids`` plus the
  Canary accounts and payees in an rng-shuffled list of plain strings: the caller cannot tell
  which entries are Canaries. ``device_population`` does the same for devices.
- The registry's id lists (``ids()``) are for in-process wiring only (generator exclusion,
  Profile_Store / Immune_System ``is_canary`` hooks); no API route exposes them.

Integration hooks (pipeline wiring is a later step):
``CanaryRegistry.generate(seed, counts)``, ``is_canary(id)`` (pass to ``ProfileStore`` and
``ImmuneSystem``), ``references(ev)`` / ``touches(ev)`` (force Case creation and Prioritiser
exemption, Req 33.3, 33.4), ``contribution(ev) -> Contribution | None`` (add to the event's
contributions before fusion), ``observe(ev)`` (record hits), ``population(base_ids, rng)``
(Attacker_Agent target pool) and ``campaign_report(campaign_of)`` (Evaluation_Monitor).
"""

from __future__ import annotations

import random
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Union

from helix.fusion import Contribution, ContributorType, Mass

CANARY_CONTRIBUTOR_ID = "canary"
CANARY_CONTRIBUTOR_PREFIX = "canary:"
CANARY_WEIGHT = 1.0
DEFAULT_FRAUD_MASS = 0.99
MIN_FRAUD_MASS = 0.95
DEFAULT_COUNT = 10
MAX_COUNT = 10_000
SEED_MAX = 4_294_967_295
GENERATOR_MAX_ACCOUNTS = 100_000  # upper bound of HelixConfig.generator.accounts
CANARY_ID_FLOOR = GENERATOR_MAX_ACCOUNTS
CANARY_ID_CEIL = 999_999
DEVICE_SUFFIXES = 3  # generator devices are DEV<6 digits>-0..2
_SALT = 0xCA9A_27


class _CanaryType(str, Enum):
    CANARY = "CANARY"

    def __str__(self) -> str:
        return self.value


#: Contributor type of Canary evidence (``ContributorType.CANARY`` once it exists).
CANARY_TYPE: Any = getattr(ContributorType, "CANARY", _CanaryType.CANARY)


class CanaryKind(str, Enum):
    ACCOUNT = "ACCOUNT"
    PAYEE = "PAYEE"
    DEVICE = "DEVICE"

    def __str__(self) -> str:
        return self.value


#: Id formats per kind: same shapes as the Synthetic_Generator's ids.
ID_FORMATS: Mapping[CanaryKind, re.Pattern[str]] = {
    CanaryKind.ACCOUNT: re.compile(r"^ACC\d{6}$"),
    CanaryKind.PAYEE: re.compile(r"^EXT\d{6}$"),
    CanaryKind.DEVICE: re.compile(r"^DEV\d{6}-[0-2]$"),
}

#: Event fields checked for Canary references: Transaction_Event and Auth_Event.
EVENT_FIELDS: tuple[str, ...] = ("src", "dst", "account_id", "payee_account_id", "device_id")


def is_canary_contributor(contributor_id: Any) -> bool:
    """True for Canary contributor ids; learners must not adjust these (Req 19.12, 33.2)."""
    return isinstance(contributor_id, str) and (
        contributor_id == CANARY_CONTRIBUTOR_ID or contributor_id.startswith(CANARY_CONTRIBUTOR_PREFIX))


class CanaryConfigError(ValueError):
    def __init__(self, field: str, value: Any, reason: str) -> None:
        self.field, self.value = field, value
        super().__init__(f"{field}: {value!r} {reason}")


@dataclass(frozen=True, slots=True)
class CanaryCounts:
    accounts: int = DEFAULT_COUNT
    payees: int = DEFAULT_COUNT
    devices: int = DEFAULT_COUNT

    def __post_init__(self) -> None:
        for name in ("accounts", "payees", "devices"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_COUNT:
                raise CanaryConfigError(f"counts.{name}", v, f"must be an integer in [0, {MAX_COUNT}]")

    def of(self, kind: CanaryKind) -> int:
        return {CanaryKind.ACCOUNT: self.accounts, CanaryKind.PAYEE: self.payees,
                CanaryKind.DEVICE: self.devices}[kind]


@dataclass(frozen=True, slots=True)
class CanaryRef:
    canary_id: str
    kind: CanaryKind
    field: str  # event field that carried the reference


@dataclass(frozen=True, slots=True)
class CanaryHit:
    canary_id: str
    kind: CanaryKind
    field: str
    event_id: str
    event_type: str  # TRANSACTION | AUTH
    ts: Optional[datetime]

    def to_dict(self) -> dict[str, Any]:
        return {"canary_id": self.canary_id, "kind": self.kind.value, "field": self.field,
                "event_id": self.event_id, "event_type": self.event_type,
                "ts": None if self.ts is None else self.ts.isoformat()}


@dataclass(frozen=True, slots=True)
class CampaignCanaries:
    """One Attack_Campaign that referenced Canaries (Req 33.8)."""

    campaign_id: str
    canaries: tuple[tuple[str, CanaryKind, Optional[datetime]], ...]  # (id, kind, first reference)
    first_reference: Optional[datetime]
    events: int

    def to_dict(self) -> dict[str, Any]:
        return {"campaign_id": self.campaign_id,
                "canaries": [{"canary_id": c, "kind": k.value, "first_reference": None if t is None else t.isoformat()}
                             for c, k, t in self.canaries],
                "first_reference": None if self.first_reference is None else self.first_reference.isoformat(),
                "events": self.events}


@dataclass(frozen=True, slots=True)
class CanaryReport:
    campaigns: tuple[CampaignCanaries, ...]
    unattributed_events: int  # Canary-touching events with no campaign (e.g. benign leakage)
    total_hits: int

    def to_dict(self) -> dict[str, Any]:
        return {"campaigns": [c.to_dict() for c in self.campaigns],
                "unattributed_events": self.unattributed_events, "total_hits": self.total_hits}


def _event_id(ev: Any) -> str:
    for k in ("tx_id", "event_id"):
        v = getattr(ev, k, None)
        if v is not None:
            return str(v)
    return ""


def _event_type(ev: Any) -> str:
    return "AUTH" if getattr(ev, "event_type", None) is not None and getattr(ev, "tx_id", None) is None \
        else "TRANSACTION"


def _ts_key(t: Optional[datetime]) -> tuple[bool, float]:
    """Sort key: known times first, in time order (aware datetimes)."""
    return (t is None, 0.0 if t is None else t.timestamp())


def _min_ts(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


class CanaryRegistry:
    """The seeded Canary set plus the hits observed against it."""

    def __init__(self, accounts: Iterable[str] = (), payees: Iterable[str] = (), devices: Iterable[str] = (),
                 *, fraud_mass: float = DEFAULT_FRAUD_MASS, seed: Optional[int] = None,
                 max_hits: int = 10_000) -> None:
        if isinstance(fraud_mass, bool) or not isinstance(fraud_mass, (int, float)) \
                or not MIN_FRAUD_MASS <= float(fraud_mass) <= 1.0:
            raise CanaryConfigError("fraud_mass", fraud_mass, f"must be a number in [{MIN_FRAUD_MASS}, 1]")
        if isinstance(max_hits, bool) or not isinstance(max_hits, int) or max_hits < 1:
            raise CanaryConfigError("max_hits", max_hits, "must be an integer >= 1")
        self.seed = seed
        self.fraud_mass = float(fraud_mass)
        self.max_hits = max_hits
        self._by_kind: dict[CanaryKind, tuple[str, ...]] = {
            CanaryKind.ACCOUNT: tuple(accounts), CanaryKind.PAYEE: tuple(payees), CanaryKind.DEVICE: tuple(devices)}
        self._kind: dict[str, CanaryKind] = {}
        for k, ids in self._by_kind.items():
            for i in ids:
                if not ID_FORMATS[k].match(i):
                    raise CanaryConfigError(f"{k.value.lower()} id", i, "does not match the generator id format")
                if i in self._kind:
                    raise CanaryConfigError(f"{k.value.lower()} id", i, "is listed twice")
                self._kind[i] = k
        self._lock = threading.Lock()
        self._hits: list[CanaryHit] = []
        self._seen: set[tuple[str, str, str]] = set()  # (event_id, field, canary_id)
        self._first: dict[str, datetime] = {}
        self._hit_count = 0
        self.dropped_hits = 0

    # -- construction ------------------------------------------------------------------

    @classmethod
    def generate(cls, seed: int, counts: Optional[CanaryCounts] = None, *, reserved: Iterable[str] = (),
                 fraud_mass: float = DEFAULT_FRAUD_MASS, max_hits: int = 10_000) -> CanaryRegistry:
        """Deterministic Canary set for ``seed`` (Req 33.1); ids in ``reserved`` are never drawn."""
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= SEED_MAX:
            raise CanaryConfigError("seed", seed, f"must be an integer in [0, {SEED_MAX}]")
        counts = counts if counts is not None else CanaryCounts()
        rng = random.Random((seed << 24) ^ _SALT)
        taken = set(reserved)
        out: dict[CanaryKind, list[str]] = {k: [] for k in CanaryKind}
        for kind in CanaryKind:  # fixed order: ACCOUNT, PAYEE, DEVICE
            while len(out[kind]) < counts.of(kind):
                n = rng.randint(CANARY_ID_FLOOR, CANARY_ID_CEIL)
                if kind is CanaryKind.ACCOUNT:
                    cid = f"ACC{n:06d}"
                elif kind is CanaryKind.PAYEE:
                    cid = f"EXT{n:06d}"
                else:
                    cid = f"DEV{n:06d}-{rng.randrange(DEVICE_SUFFIXES)}"
                if cid not in taken:
                    taken.add(cid)
                    out[kind].append(cid)
        return cls(out[CanaryKind.ACCOUNT], out[CanaryKind.PAYEE], out[CanaryKind.DEVICE],
                   fraud_mass=fraud_mass, seed=seed, max_hits=max_hits)

    # -- membership --------------------------------------------------------------------

    def is_canary(self, identifier: Any) -> bool:
        return isinstance(identifier, str) and identifier in self._kind

    __contains__ = is_canary

    def predicate(self) -> Callable[[Any], bool]:
        """A read-only ``is_canary`` closure over the (immutable) id set, for the Profile_Store
        and Immune_System hooks (Req 33.6). A plain function, so ``copy.deepcopy`` of a holder
        (Purple_Team isolation) shares it instead of copying the registry and its lock."""
        ids = frozenset(self._kind)

        def is_canary(identifier: Any) -> bool:
            return isinstance(identifier, str) and identifier in ids

        return is_canary

    def kind_of(self, identifier: str) -> Optional[CanaryKind]:
        return self._kind.get(identifier)

    def ids(self, kind: Optional[CanaryKind] = None) -> tuple[str, ...]:
        """Canary ids (in-process wiring only; never serve these over the API)."""
        if kind is not None:
            return self._by_kind[CanaryKind(kind)]
        return tuple(i for k in CanaryKind for i in self._by_kind[k])

    def __len__(self) -> int:
        return len(self._kind)

    def counts(self) -> dict[str, int]:
        return {k.value: len(v) for k, v in self._by_kind.items()}

    # -- events ------------------------------------------------------------------------

    def references(self, ev: Any) -> tuple[CanaryRef, ...]:
        """Canary references of a Transaction_Event or Auth_Event (field order of ``EVENT_FIELDS``)."""
        out = []
        for f in EVENT_FIELDS:
            v = getattr(ev, f, None)
            k = self._kind.get(v) if isinstance(v, str) else None
            if k is not None:
                out.append(CanaryRef(v, k, f))
        return tuple(out)

    def touches(self, ev: Any) -> bool:
        """True iff ``ev`` references a Canary (force a Case, skip DNA / Self_Set, Req 33.3, 33.6)."""
        return any(isinstance(v := getattr(ev, f, None), str) and v in self._kind for f in EVENT_FIELDS)

    def mass(self) -> Mass:
        return Mass(self.fraud_mass, 0.0, 1.0 - self.fraud_mass)

    def contribution(self, ev: Any) -> Optional[Contribution]:
        """Canary evidence for ``ev``: ``{FRAUD}`` mass >= 0.95, weight 1.0, kind CANARY (Req 33.2);
        None when the event references no Canary. Pure: records nothing (see ``observe``)."""
        if not self.touches(ev):
            return None
        return Contribution(CANARY_CONTRIBUTOR_ID, CANARY_TYPE, self.mass(), CANARY_WEIGHT)

    def observe(self, ev: Any) -> tuple[CanaryHit, ...]:
        """Record the event's Canary references as hits (each (event, field, Canary) once)."""
        refs = self.references(ev)
        if not refs:
            return ()
        eid, etype, ts = _event_id(ev), _event_type(ev), getattr(ev, "ts", None)
        ts = ts if isinstance(ts, datetime) else None
        new = []
        with self._lock:
            for r in refs:
                key = (eid, r.field, r.canary_id)
                if key in self._seen:
                    continue
                self._seen.add(key)
                h = CanaryHit(r.canary_id, r.kind, r.field, eid, etype, ts)
                self._hit_count += 1
                if ts is not None:
                    first = self._first.get(r.canary_id)
                    if first is None or ts < first:
                        self._first[r.canary_id] = ts
                if len(self._hits) < self.max_hits:
                    self._hits.append(h)
                else:
                    self.dropped_hits += 1
                new.append(h)
        return tuple(new)

    # -- hits and reports ----------------------------------------------------------------

    def hits(self, limit: Optional[int] = None) -> list[CanaryHit]:
        with self._lock:
            hs = list(self._hits)
        return hs if limit is None else hs[-limit:]

    @property
    def hit_count(self) -> int:
        return self._hit_count

    def first_reference(self, canary_id: str) -> Optional[datetime]:
        with self._lock:
            return self._first.get(canary_id)

    def touched(self) -> list[dict[str, Any]]:
        """One row per touched Canary: id, kind, first reference, hit count (stored hits)."""
        with self._lock:
            hs = list(self._hits)
            first = dict(self._first)
        rows: dict[str, dict[str, Any]] = {}
        for h in hs:
            r = rows.setdefault(h.canary_id, {"canary_id": h.canary_id, "kind": h.kind.value,
                                              "first_reference": first.get(h.canary_id), "hits": 0})
            r["hits"] += 1
        return sorted(rows.values(), key=lambda r: (*_ts_key(r["first_reference"]), r["canary_id"]))

    def campaign_report(self, campaign_of: Union[Callable[[str], Optional[str]], Mapping[str, str]]) -> CanaryReport:
        """Attack_Campaigns that referenced a Canary, with the Canary ids and first reference
        times (Req 33.8). ``campaign_of(event_id)`` comes from the Evaluation_Monitor."""
        look: Callable[[str], Optional[str]] = (campaign_of.get if isinstance(campaign_of, Mapping)
                                                else campaign_of)
        with self._lock:
            hs = list(self._hits)
        per: dict[str, dict[str, tuple[CanaryKind, Optional[datetime]]]] = {}
        events: dict[str, set[str]] = {}
        unattributed: set[str] = set()
        for h in hs:
            cid = look(h.event_id)
            if cid is None:
                unattributed.add(h.event_id)
                continue
            cmap = per.setdefault(str(cid), {})
            k, t = cmap.get(h.canary_id, (h.kind, None))
            cmap[h.canary_id] = (k, _min_ts(t, h.ts))
            events.setdefault(str(cid), set()).add(h.event_id)
        camps = []
        for cid, cmap in per.items():
            rows = tuple(sorted(((c, k, t) for c, (k, t) in cmap.items()), key=lambda r: (*_ts_key(r[2]), r[0])))
            first = None
            for _, _, t in rows:
                first = _min_ts(first, t)
            camps.append(CampaignCanaries(cid, rows, first, len(events[cid])))
        camps.sort(key=lambda c: (*_ts_key(c.first_reference), c.campaign_id))
        return CanaryReport(tuple(camps), len(unattributed), len(hs))

    # -- attacker population -------------------------------------------------------------

    def population(self, base_ids: Iterable[str], rng: Any,
                   kinds: Iterable[CanaryKind] = (CanaryKind.ACCOUNT, CanaryKind.PAYEE)) -> list[str]:
        """``base_ids`` plus the Canaries of ``kinds``, shuffled by ``rng`` (anything with
        ``shuffle(list)``: ``random.Random`` or ``numpy.random.Generator``). Plain strings in
        random order: the caller cannot tell which are Canaries (Req 33.7)."""
        out = list(dict.fromkeys(base_ids))
        have = set(out)
        for k in kinds:
            out += [i for i in self._by_kind[CanaryKind(k)] if i not in have]
        rng.shuffle(out)
        return [str(x) for x in out]

    def device_population(self, base_ids: Iterable[str], rng: Any) -> list[str]:
        return self.population(base_ids, rng, kinds=(CanaryKind.DEVICE,))


__all__ = [
    "CANARY_CONTRIBUTOR_ID",
    "CANARY_CONTRIBUTOR_PREFIX",
    "CANARY_ID_FLOOR",
    "CANARY_TYPE",
    "CANARY_WEIGHT",
    "DEFAULT_FRAUD_MASS",
    "EVENT_FIELDS",
    "GENERATOR_MAX_ACCOUNTS",
    "ID_FORMATS",
    "MIN_FRAUD_MASS",
    "CampaignCanaries",
    "CanaryConfigError",
    "CanaryCounts",
    "CanaryHit",
    "CanaryKind",
    "CanaryRef",
    "CanaryRegistry",
    "CanaryReport",
    "is_canary_contributor",
]
