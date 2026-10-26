"""Threat-intelligence Indicator_Store (Req 34): Indicators from confirmed fraud, matched
against later events and shared as STIX 2.1-style bundles.

Indicator: ``value``, ``type`` (ACCOUNT | DEVICE | IP | MERCHANT), ``confidence`` (integer
0-100, as in STIX), ``source``, ``first_seen``, ``last_seen``, ``expiry`` (aware datetimes,
normalised to UTC) and ``tlp`` (TLP:WHITE | TLP:GREEN | TLP:AMBER | TLP:RED). An Indicator is
keyed by ``(type, value)``.

Design decisions:
- Extraction (Req 34.2, ``on_label``): only a CONFIRMED_FRAUD label extracts. The Case's
  ``accounts`` become ACCOUNT Indicators. Its events come from ``events=`` (explicit), else
  ``case.events`` when the object has it, else ``resolver(tx_id)`` per ``case.tx_ids``; each
  event's ``device_id`` gives a DEVICE, ``ip`` an IP (normalised with ``ipaddress``),
  ``merchant_id`` / ``merchant`` a MERCHANT, and ``dst`` of an event that carries an ``mcc``
  (a card payment, so ``dst`` is the merchant) a MERCHANT too. The MCC itself is *not* an
  Indicator: a category code would match every transaction of that category. New Indicators get
  the configured defaults (confidence 75, expiry 30 days from the label time, TLP:AMBER,
  ``source`` "helix:<case_id>"); an existing Indicator only gets ``last_seen`` moved forward
  (its expiry, confidence and marking are left alone, as the requirement states).
- Matching (Req 34.3, 34.4, ``matches`` / ``contribution``): the values of an event
  (``src``, ``dst``, ``account_id`` as ACCOUNT; ``device_id``; ``ip``; ``merchant_id`` /
  ``merchant`` / ``dst`` with ``mcc`` as MERCHANT) are looked up by key, O(1) each. An
  Indicator whose ``expiry`` is earlier than the event's ``ts`` is excluded. The event gets one
  Mass_Function from the best matching Indicator (highest confidence, then type, then value):
  ``m({FRAUD}) = base_mass * confidence / 100``, ``m(Θ) = 1 - m({FRAUD})``, ``m({LEGIT}) = 0``,
  as one ``Contribution("indicator", ContributorType.INDICATOR, ...)``. One contributor per
  event avoids counting the same mule twice when it appears as both ACCOUNT and MERCHANT; its
  Sensor_Weight is ``weights["indicator"]`` (the Feedback_Learner's ``other_weights``) or 1.
- STIX 2.1-style export (Req 34.5, 34.8): a ``bundle`` with the TLP ``marking-definition``
  objects in use (the predefined STIX 2.1 TLP ids) and one ``indicator`` per Indicator:
  deterministic ``id`` (UUIDv5 of type and value), ``pattern`` (``[user-account:user_id =
  '...']``, ``[x-helix-device:device_id = '...']``, ``[ipv4-addr:value = '...']`` /
  ``ipv6-addr``, ``[x-helix-merchant:merchant_id = '...']``; quotes and backslashes escaped),
  ``created`` / ``valid_from`` = first_seen, ``modified`` = last_seen, ``valid_until`` = expiry,
  ``confidence``, ``object_marking_refs`` and ``x_helix_source``. Timestamps carry microseconds,
  so export then import reproduces every field exactly. TLP:RED Indicators are left out unless
  ``include_red=True``.
- Import (Req 34.6, 34.7): every ``indicator`` object is validated on its own; one with a
  missing or invalid field is rejected and reported as ``{"id", "errors": [{field, reason}]}``
  with every invalid field, while valid objects are imported (upsert by key, the imported
  Indicator replaces a stored one). ``x_helix_source`` is optional (fallback ``created_by_ref``,
  then "stix-import"); everything else that maps to an Indicator field is required. Other STIX
  object types are counted as skipped. A document that is not a bundle raises ``BundleError``.
- Persistence is optional (``storage=``): table ``soc_indicators`` keyed by type and value,
  upserted on every change and loaded at start. A storage fault is logged and counted in
  ``storage_errors``; the in-memory store stays authoritative.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Union

from helix.fusion import Contribution, ContributorType, FusedAssessment, Mass, fuse
from helix.sensors.base import SensorOutput
from helix.sensors.runner import SensorOutcome, SensorResult

from .ledger import canonical_json, check_identifier, connection_scope, format_ts, parse_ts

log = logging.getLogger(__name__)

CONTRIBUTOR_ID = "indicator"
MAX_VALUE = 256
MAX_SOURCE = 256
MAX_OBJECTS = 10_000
STIX_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://helix.local/stix/indicator")
_STIX_ID = re.compile(r"^indicator--[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_PATTERN = re.compile(r"^\[([a-z0-9-]+:[a-z_]+) = '((?:[^'\\]|\\['\\])*)'\]$")


class IndicatorType(str, Enum):
    ACCOUNT = "ACCOUNT"
    DEVICE = "DEVICE"
    IP = "IP"
    MERCHANT = "MERCHANT"

    def __str__(self) -> str:
        return self.value


class TLP(str, Enum):
    WHITE = "TLP:WHITE"
    GREEN = "TLP:GREEN"
    AMBER = "TLP:AMBER"
    RED = "TLP:RED"

    def __str__(self) -> str:
        return self.value


#: Predefined STIX 2.1 TLP marking-definition ids (STIX 2.1 §7.2.1.4).
TLP_MARKING_IDS: dict[TLP, str] = {
    TLP.WHITE: "marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9",
    TLP.GREEN: "marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da",
    TLP.AMBER: "marking-definition--f88d31f6-486f-44da-b317-01333bde0b82",
    TLP.RED: "marking-definition--5e57c739-391a-4eb3-b6be-7d15ca92d5ed",
}
_TLP_BY_ID = {v: k for k, v in TLP_MARKING_IDS.items()}
_TLP_BY_WORD = {t.value.split(":", 1)[1].lower(): t for t in TLP}
TLP_CREATED = "2017-01-20T00:00:00.000Z"

#: STIX object path per Indicator type (IP: by address version).
PATHS: dict[str, IndicatorType] = {
    "user-account:user_id": IndicatorType.ACCOUNT,
    "x-helix-device:device_id": IndicatorType.DEVICE,
    "ipv4-addr:value": IndicatorType.IP,
    "ipv6-addr:value": IndicatorType.IP,
    "x-helix-merchant:merchant_id": IndicatorType.MERCHANT,
}


class IndicatorError(ValueError):
    """Invalid Indicator; ``problems`` lists ``(field, reason)``."""

    def __init__(self, problems: Sequence[tuple[str, str]]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(f"{f}: {r}" for f, r in self.problems))


class BundleError(ValueError):
    """The imported document is not a STIX bundle; nothing was imported."""


# --- Indicator --------------------------------------------------------------------------


def normalize_ip(v: Any) -> Optional[str]:
    """Canonical text of an IPv4 / IPv6 address, or None when ``v`` is not one."""
    if not isinstance(v, str):
        return None
    try:
        return str(ipaddress.ip_address(v.strip()))
    except ValueError:
        return None


def value_problem(t: IndicatorType, v: Any) -> Optional[str]:
    if not isinstance(v, str) or not v:
        return "must be a non-empty string"
    if len(v) > MAX_VALUE:
        return f"longer than {MAX_VALUE} characters"
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        return "contains control characters"
    if v != v.strip():
        return "has leading or trailing whitespace"
    if t is IndicatorType.IP and normalize_ip(v) != v:
        return "is not a canonical IPv4 or IPv6 address"
    return None


def _aware(v: Any) -> bool:
    return isinstance(v, datetime) and v.tzinfo is not None and v.utcoffset() is not None


def indicator_problems(value: Any, type: Any, confidence: Any, source: Any, first_seen: Any,  # noqa: A002
                       last_seen: Any, expiry: Any, tlp: Any) -> list[tuple[str, str]]:
    """Every invalid field of a would-be Indicator, as ``(field, reason)``."""
    out: list[tuple[str, str]] = []
    try:
        t: Optional[IndicatorType] = IndicatorType(type)
    except ValueError:
        t = None
        out.append(("type", f"must be one of {[x.value for x in IndicatorType]}"))
    if t is not None and (p := value_problem(t, value)) is not None:
        out.append(("value", p))
    if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
        out.append(("confidence", "must be an integer in [0, 100]"))
    if not isinstance(source, str) or not source.strip() or len(source) > MAX_SOURCE:
        out.append(("source", f"must be a non-empty string of at most {MAX_SOURCE} characters"))
    for n, v in (("first_seen", first_seen), ("last_seen", last_seen), ("expiry", expiry)):
        if not _aware(v):
            out.append((n, "must be a timezone-aware datetime"))
    if _aware(first_seen) and _aware(last_seen) and last_seen < first_seen:
        out.append(("last_seen", "is earlier than first_seen"))
    if _aware(first_seen) and _aware(expiry) and expiry <= first_seen:
        out.append(("expiry", "must be later than first_seen"))
    try:
        TLP(tlp)
    except ValueError:
        out.append(("tlp", f"must be one of {[x.value for x in TLP]}"))
    return out


def _utc(d: datetime) -> datetime:
    return d.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Indicator:
    value: str
    type: IndicatorType
    confidence: int  # 0-100
    source: str
    first_seen: datetime
    last_seen: datetime
    expiry: datetime
    tlp: TLP = TLP.AMBER

    def __post_init__(self) -> None:
        probs = indicator_problems(self.value, self.type, self.confidence, self.source, self.first_seen,
                                   self.last_seen, self.expiry, self.tlp)
        if probs:
            raise IndicatorError(probs)
        object.__setattr__(self, "type", IndicatorType(self.type))
        object.__setattr__(self, "tlp", TLP(self.tlp))
        for n in ("first_seen", "last_seen", "expiry"):
            object.__setattr__(self, n, _utc(getattr(self, n)))

    @property
    def key(self) -> tuple[IndicatorType, str]:
        return (self.type, self.value)

    @property
    def stix_id(self) -> str:
        return f"indicator--{uuid.uuid5(STIX_NAMESPACE, f'{self.type.value}:{self.value}')}"

    def expired_at(self, ts: datetime) -> bool:
        """True when ``ts`` is later than the expiry (Req 34.4)."""
        return ts > self.expiry

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "type": self.type.value, "confidence": self.confidence,
                "source": self.source, "first_seen": format_ts(self.first_seen),
                "last_seen": format_ts(self.last_seen), "expiry": format_ts(self.expiry),
                "tlp": self.tlp.value}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Indicator:
        return cls(d["value"], IndicatorType(d["type"]), int(d["confidence"]), d["source"],
                   parse_ts(d["first_seen"]), parse_ts(d["last_seen"]), parse_ts(d["expiry"]), TLP(d["tlp"]))


@dataclass(frozen=True, slots=True)
class IndicatorConfig:
    default_confidence: int = 75
    default_expiry_days: float = 30.0
    default_tlp: TLP = TLP.AMBER
    base_mass: float = 0.8

    def __post_init__(self) -> None:
        c = self.default_confidence
        if isinstance(c, bool) or not isinstance(c, int) or not 0 <= c <= 100:
            raise ValueError(f"default_confidence must be an integer in [0, 100], got {c!r}")
        d = self.default_expiry_days
        if not (isinstance(d, (int, float)) and math.isfinite(d) and 0 < d <= 3650):
            raise ValueError(f"default_expiry_days must be in (0, 3650], got {d!r}")
        b = self.base_mass
        if not (isinstance(b, (int, float)) and math.isfinite(b) and 0.0 <= b <= 1.0):
            raise ValueError(f"base_mass must be in [0, 1], got {b!r}")
        object.__setattr__(self, "default_tlp", TLP(self.default_tlp))

    def to_dict(self) -> dict[str, Any]:
        return {"default_confidence": self.default_confidence, "default_expiry_days": self.default_expiry_days,
                "default_tlp": self.default_tlp.value, "base_mass": self.base_mass}


def indicator_mass(confidence: int, base_mass: float = 0.8) -> Mass:
    """``m({FRAUD}) = base_mass * confidence / 100``; the rest is Uncertainty_Mass (Req 34.3)."""
    f = float(base_mass) * float(confidence) / 100.0
    return Mass(f, 0.0, 1.0 - f)


# --- event values ---------------------------------------------------------------------------


def _str(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and v else None


def event_keys(ev: Any) -> list[tuple[IndicatorType, str]]:
    """Indicator keys an event (Transaction_Event or Auth_Event, duck-typed) carries."""
    out: list[tuple[IndicatorType, str]] = []
    for a in ("src", "dst", "account_id"):
        if (v := _str(getattr(ev, a, None))) is not None:
            out.append((IndicatorType.ACCOUNT, v))
    if (v := _str(getattr(ev, "device_id", None))) is not None:
        out.append((IndicatorType.DEVICE, v))
    if (v := normalize_ip(getattr(ev, "ip", None))) is not None:
        out.append((IndicatorType.IP, v))
    for a in ("merchant_id", "merchant"):
        if (v := _str(getattr(ev, a, None))) is not None:
            out.append((IndicatorType.MERCHANT, v))
    if _str(getattr(ev, "mcc", None)) is not None and (v := _str(getattr(ev, "dst", None))) is not None:
        out.append((IndicatorType.MERCHANT, v))
    seen: set[tuple[IndicatorType, str]] = set()
    return [k for k in out if not (k in seen or seen.add(k))]  # type: ignore[func-returns-value]


def _is_confirmed(label: Any) -> bool:
    v = getattr(label, "value", label)
    return isinstance(v, str) and v.upper() == "CONFIRMED_FRAUD"


# --- STIX 2.1-style bundles -------------------------------------------------------------------


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("'", "\\'")


def _unescape(v: str) -> str:
    return re.sub(r"\\(['\\])", r"\1", v)


def stix_pattern(ind: Indicator) -> str:
    if ind.type is IndicatorType.IP:
        path = "ipv6-addr:value" if ipaddress.ip_address(ind.value).version == 6 else "ipv4-addr:value"
    else:
        path = next(p for p, t in PATHS.items() if t is ind.type)
    return f"[{path} = '{_escape(ind.value)}']"


def parse_pattern(pattern: Any) -> tuple[IndicatorType, str]:
    """``(type, value)`` of a pattern produced by ``stix_pattern``. Raises ValueError."""
    m = _PATTERN.match(pattern) if isinstance(pattern, str) else None
    if m is None:
        raise ValueError("must be a single comparison like [user-account:user_id = 'value']")
    path, raw = m.groups()
    t = PATHS.get(path)
    if t is None:
        raise ValueError(f"unsupported object path {path!r}; expected one of {sorted(PATHS)}")
    v = _unescape(raw)
    if t is IndicatorType.IP:
        ip = normalize_ip(v)
        want = None if ip is None else ("ipv6-addr:value" if ipaddress.ip_address(ip).version == 6 else "ipv4-addr:value")
        if want is not None and want != path:
            raise ValueError(f"address {v!r} does not match object path {path!r}")
    return t, v


def marking_definition(t: TLP) -> dict[str, Any]:
    word = t.value.split(":", 1)[1].lower()
    return {"type": "marking-definition", "spec_version": "2.1", "id": TLP_MARKING_IDS[t],
            "created": TLP_CREATED, "definition_type": "tlp", "name": t.value, "definition": {"tlp": word}}


def stix_ts(d: datetime) -> str:
    return format_ts(d)


def to_stix(ind: Indicator) -> dict[str, Any]:
    return {
        "type": "indicator",
        "spec_version": "2.1",
        "id": ind.stix_id,
        "created": stix_ts(ind.first_seen),
        "modified": stix_ts(ind.last_seen),
        "name": f"{ind.type.value} {ind.value}",
        "indicator_types": ["malicious-activity"],
        "pattern": stix_pattern(ind),
        "pattern_type": "stix",
        "valid_from": stix_ts(ind.first_seen),
        "valid_until": stix_ts(ind.expiry),
        "confidence": ind.confidence,
        "object_marking_refs": [TLP_MARKING_IDS[ind.tlp]],
        "x_helix_source": ind.source,
    }


def to_bundle(indicators: Iterable[Indicator], *, include_red: bool = False) -> dict[str, Any]:
    """STIX 2.1-style bundle of ``indicators`` (TLP:RED only with ``include_red``; Req 34.5, 34.8)."""
    inds = sorted((i for i in indicators if include_red or i.tlp is not TLP.RED),
                  key=lambda i: (i.type.value, i.value))
    used = sorted({i.tlp for i in inds}, key=lambda t: list(TLP).index(t))
    objs = [marking_definition(t) for t in used] + [to_stix(i) for i in inds]
    bid = uuid.uuid5(STIX_NAMESPACE, canonical_json([o["id"] for o in objs]) + canonical_json(
        [[o.get("modified"), o.get("confidence")] for o in objs]))
    return {"type": "bundle", "id": f"bundle--{bid}", "objects": objs}


@dataclass(frozen=True, slots=True)
class RejectedObject:
    object_id: str
    errors: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.object_id, "errors": [{"field": f, "reason": r} for f, r in self.errors]}


@dataclass(frozen=True, slots=True)
class ImportReport:
    imported: tuple[Indicator, ...] = ()
    rejected: tuple[RejectedObject, ...] = ()
    skipped: int = 0  # non-indicator objects (marking definitions included)

    def to_dict(self) -> dict[str, Any]:
        return {"imported": len(self.imported), "rejected": [r.to_dict() for r in self.rejected],
                "skipped": self.skipped, "indicators": [i.to_dict() for i in self.imported]}


def _ts_field(obj: Mapping[str, Any], name: str, errs: list[tuple[str, str]]) -> Optional[datetime]:
    if name not in obj:
        errs.append((name, "missing"))
        return None
    try:
        return parse_ts(obj[name])
    except ValueError as e:
        errs.append((name, str(e)))
        return None


def _bundle_markings(objects: Sequence[Any]) -> dict[str, TLP]:
    out = dict(_TLP_BY_ID)
    for o in objects:
        if isinstance(o, Mapping) and o.get("type") == "marking-definition" and isinstance(o.get("id"), str):
            d = o.get("definition")
            word = d.get("tlp") if isinstance(d, Mapping) else None
            if o.get("definition_type") == "tlp" and isinstance(word, str) and word.lower() in _TLP_BY_WORD:
                out.setdefault(o["id"], _TLP_BY_WORD[word.lower()])
    return out


def parse_indicator_object(obj: Any, markings: Mapping[str, TLP], position: int = 0
                           ) -> Union[Indicator, RejectedObject]:
    """One STIX ``indicator`` object -> Indicator, or the rejection with every invalid field."""
    if not isinstance(obj, Mapping):
        return RejectedObject(f"objects[{position}]", (("object", "must be a JSON object"),))
    oid = obj.get("id")
    ref = oid if isinstance(oid, str) and oid else f"objects[{position}]"
    errs: list[tuple[str, str]] = []
    if not isinstance(oid, str) or not _STIX_ID.match(oid):
        errs.append(("id", "missing" if oid is None else "must be indicator--<uuid>"))
    if obj.get("spec_version") != "2.1":
        errs.append(("spec_version", "missing" if "spec_version" not in obj else "must be '2.1'"))
    if obj.get("pattern_type") != "stix":
        errs.append(("pattern_type", "missing" if "pattern_type" not in obj else "must be 'stix'"))
    t: Optional[IndicatorType] = None
    value: Any = None
    if "pattern" not in obj:
        errs.append(("pattern", "missing"))
    else:
        try:
            t, value = parse_pattern(obj["pattern"])
            if (p := value_problem(t, value)) is not None:
                errs.append(("pattern", f"value {p}"))
        except ValueError as e:
            errs.append(("pattern", str(e)))
    _ts_field(obj, "created", errs)
    first = _ts_field(obj, "valid_from", errs)
    expiry = _ts_field(obj, "valid_until", errs)
    last = _ts_field(obj, "modified", errs)
    if first is not None and expiry is not None and expiry <= first:
        errs.append(("valid_until", "must be later than valid_from"))
    if first is not None and last is not None and last < first:
        errs.append(("modified", "must not be earlier than valid_from"))
    conf = obj.get("confidence")
    if "confidence" not in obj:
        errs.append(("confidence", "missing"))
    elif isinstance(conf, bool) or not isinstance(conf, int) or not 0 <= conf <= 100:
        errs.append(("confidence", "must be an integer in [0, 100]"))
    tlp: Optional[TLP] = None
    refs = obj.get("object_marking_refs")
    if refs is None:
        errs.append(("object_marking_refs", "missing (a TLP marking is required)"))
    elif not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        errs.append(("object_marking_refs", "must be a list of marking-definition ids"))
    else:
        tlps = {markings[r] for r in refs if r in markings}
        if len(tlps) != 1:
            errs.append(("object_marking_refs", "must reference exactly one TLP marking definition"
                         + ("" if tlps else " (none recognised)")))
        else:
            tlp = next(iter(tlps))
    src = obj.get("x_helix_source", obj.get("created_by_ref", "stix-import"))
    if not isinstance(src, str) or not src.strip() or len(src) > MAX_SOURCE:
        errs.append(("x_helix_source", f"must be a non-empty string of at most {MAX_SOURCE} characters"))
    if errs:
        return RejectedObject(ref, tuple(errs))
    try:  # a foreign (non-UUIDv5) id is accepted: the stored Indicator is keyed by (type, value)
        return Indicator(value, t, conf, src, first, last, expiry, tlp)  # type: ignore[arg-type]
    except IndicatorError as e:
        return RejectedObject(ref, e.problems)


def parse_bundle(doc: Any) -> ImportReport:
    """Parse a STIX 2.1-style bundle (Req 34.6, 34.7) without touching any store."""
    if isinstance(doc, (str, bytes, bytearray)):
        try:
            doc = json.loads(doc)
        except ValueError as e:
            raise BundleError(f"not JSON: {e}") from None
    if not isinstance(doc, Mapping) or doc.get("type") != "bundle":
        raise BundleError("document must be a JSON object with type 'bundle'")
    objects = doc.get("objects", [])
    if not isinstance(objects, list):
        raise BundleError("bundle 'objects' must be a list")
    if len(objects) > MAX_OBJECTS:
        raise BundleError(f"bundle has more than {MAX_OBJECTS} objects")
    markings = _bundle_markings(objects)
    imported: list[Indicator] = []
    rejected: list[RejectedObject] = []
    skipped = 0
    for n, obj in enumerate(objects):
        if isinstance(obj, Mapping) and obj.get("type") != "indicator":
            skipped += 1
            continue
        r = parse_indicator_object(obj, markings, n)
        (imported if isinstance(r, Indicator) else rejected).append(r)  # type: ignore[arg-type]
    return ImportReport(tuple(imported), tuple(rejected), skipped)


# --- persistence ------------------------------------------------------------------------------


class DuckDBIndicatorBackend:
    """Indicators in table ``soc_indicators`` (upsert by type and value)."""

    def __init__(self, target: Any, table: str = "soc_indicators") -> None:
        self.target = target
        self.table = check_identifier(table)
        self._lock = threading.RLock()
        with connection_scope(self.target, self._lock) as con:
            con.execute(f"CREATE TABLE IF NOT EXISTS {self.table} (ind_type VARCHAR NOT NULL, "
                        "value VARCHAR NOT NULL, payload VARCHAR NOT NULL, PRIMARY KEY (ind_type, value))")

    def load(self) -> list[Indicator]:
        with connection_scope(self.target, self._lock) as con:
            rows = con.execute(f"SELECT payload FROM {self.table} ORDER BY ind_type, value").fetchall()
        out = []
        for (p,) in rows:
            try:
                out.append(Indicator.from_dict(json.loads(p)))
            except (ValueError, KeyError, TypeError):
                log.warning("intel: skipping an unreadable stored Indicator row")
        return out

    def write(self, inds: Sequence[Indicator]) -> None:
        if not inds:
            return
        with connection_scope(self.target, self._lock) as con:
            for i in inds:
                con.execute(f"INSERT OR REPLACE INTO {self.table} VALUES (?, ?, ?)",
                            [i.type.value, i.value, canonical_json(i.to_dict())])


# --- the store ----------------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IndicatorStore:
    """Stores, extracts, matches, exports and imports Indicators (Req 34)."""

    def __init__(self, cfg: Optional[IndicatorConfig] = None, *, clock: Callable[[], datetime] = _utcnow,
                 resolver: Optional[Callable[[str], Any]] = None, storage: Any = None,
                 weights: Optional[Mapping[str, float]] = None) -> None:
        """``clock``: label time for extraction (stream time in the pipeline); ``resolver``:
        tx_id -> event (or None) for Cases without ``events``; ``storage``: a ``Storage`` or
        DuckDB connection for the ``soc_indicators`` table; ``weights``: learned weights."""
        self.cfg = cfg or IndicatorConfig()
        self.clock = clock
        self.resolver = resolver
        self.weights: Mapping[str, float] = weights if weights is not None else {}
        self.backend = DuckDBIndicatorBackend(storage) if storage is not None else None
        self.storage_errors = 0
        self.matches_total = 0
        self._lock = threading.RLock()
        self._by_key: dict[tuple[IndicatorType, str], Indicator] = {}
        if self.backend is not None:
            for i in self.backend.load():
                self._by_key[i.key] = i

    def copy(self) -> IndicatorStore:
        """Independent in-memory copy (Purple_Team_Exercise isolation): same config,
        clock and Indicators, a snapshot of the weights, no Storage backend and no resolver."""
        out = IndicatorStore(self.cfg, clock=self.clock, weights=dict(self.weights))
        with self._lock:
            out._by_key = dict(self._by_key)  # Indicators are frozen
        return out

    # -- queries -------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._by_key)

    def get(self, type: Union[IndicatorType, str], value: str) -> Optional[Indicator]:  # noqa: A002
        return self._by_key.get((IndicatorType(type), value))

    def all(self) -> list[Indicator]:
        with self._lock:
            return sorted(self._by_key.values(), key=lambda i: (i.type.value, i.value))

    # -- mutations -------------------------------------------------------------------------

    def _persist(self, inds: Sequence[Indicator]) -> None:
        if self.backend is None:
            return
        try:
            self.backend.write(inds)
        except Exception:  # noqa: BLE001 - storage faults must not break labels or imports
            self.storage_errors += 1
            log.exception("intel: could not persist %d Indicator(s)", len(inds))

    def add(self, ind: Indicator) -> Indicator:
        """Insert or replace by ``(type, value)``."""
        with self._lock:
            self._by_key[ind.key] = ind
        self._persist([ind])
        return ind

    def observe(self, type: Union[IndicatorType, str], value: str, now: datetime,  # noqa: A002
                source: str) -> Indicator:
        """Create with the defaults, or move ``last_seen`` of an existing Indicator (Req 34.2)."""
        t = IndicatorType(type)
        if t is IndicatorType.IP:
            value = normalize_ip(value) or value
        with self._lock:
            old = self._by_key.get((t, value))
            if old is None:
                c = self.cfg
                ind = Indicator(value, t, c.default_confidence, source, now, now,
                                now + timedelta(days=c.default_expiry_days), c.default_tlp)
            else:
                ind = replace(old, last_seen=max(old.last_seen, _utc(now)))
            self._by_key[ind.key] = ind
            return ind

    def _case_events(self, case: Any, events: Optional[Iterable[Any]]) -> list[Any]:
        if events is not None:
            return list(events)
        evs = getattr(case, "events", None)
        if evs is not None:
            return list(evs)
        if self.resolver is None:
            return []
        out = []
        for tx in sorted(getattr(case, "tx_ids", ()) or ()):
            try:
                ev = self.resolver(tx)
            except Exception:  # noqa: BLE001 - an unknown tx is simply not extracted
                ev = None
            if ev is not None:
                out.append(ev)
        return out

    def extract_keys(self, case: Any, events: Optional[Iterable[Any]] = None) -> list[tuple[IndicatorType, str]]:
        """Indicator keys of a Case: its accounts plus device / IP / merchant values of its events."""
        keys: list[tuple[IndicatorType, str]] = [
            (IndicatorType.ACCOUNT, a) for a in sorted(getattr(case, "accounts", ()) or ()) if _str(a)]
        for ev in self._case_events(case, events):
            if (v := _str(getattr(ev, "device_id", None))) is not None:
                keys.append((IndicatorType.DEVICE, v))
            if (v := normalize_ip(getattr(ev, "ip", None))) is not None:
                keys.append((IndicatorType.IP, v))
            for k in event_keys(ev):
                if k[0] is IndicatorType.MERCHANT:
                    keys.append(k)
        seen: set[tuple[IndicatorType, str]] = set()
        return [k for k in keys if not (k in seen or seen.add(k))]  # type: ignore[func-returns-value]

    def on_label(self, case: Any, label: Any, *, now: Optional[datetime] = None,
                 events: Optional[Iterable[Any]] = None) -> list[Indicator]:
        """Feedback hook (Req 34.2): a CONFIRMED_FRAUD label extracts the Case's Indicators.
        Returns the created / updated Indicators (empty for any other label)."""
        if not _is_confirmed(label):
            return []
        at = now if now is not None else self.clock()
        source = f"helix:{getattr(case, 'case_id', 'case')}"
        out = []
        for t, v in self.extract_keys(case, events):
            if value_problem(t, v) is not None:
                continue  # e.g. an id with control characters cannot be an Indicator
            out.append(self.observe(t, v, at, source))
        self._persist(out)
        return out

    def feedback_hook(self, clock: Optional[Callable[[], datetime]] = None) -> Callable[[Any, Any], None]:
        """``Helix.feedback_hooks`` callable ``(case, label)``; never raises."""

        def hook(case: Any, label: Any) -> None:
            try:
                self.on_label(case, label, now=None if clock is None else clock())
            except Exception:  # noqa: BLE001 - learning must never break the label request
                log.exception("intel: label hook failed for %s", getattr(case, "case_id", "?"))

        hook.indicator_store = self  # type: ignore[attr-defined]
        return hook

    # -- matching (Req 34.3, 34.4) --------------------------------------------------------------

    def matches(self, ev: Any) -> list[Indicator]:
        """Unexpired Indicators the event's values match, best first."""
        ts = getattr(ev, "ts", None)
        if not _aware(ts):
            return []
        by_key = self._by_key
        hits = [i for k in event_keys(ev) if (i := by_key.get(k)) is not None and not i.expired_at(ts)]
        hits.sort(key=lambda i: (-i.confidence, i.type.value, i.value))
        return hits

    def weight(self) -> float:
        w = self.weights.get(CONTRIBUTOR_ID, 1.0)
        try:
            w = float(w)
        except (TypeError, ValueError):
            return 1.0
        return min(1.0, max(0.0, w)) if math.isfinite(w) else 1.0

    def contribution(self, ev: Any) -> Optional[Contribution]:
        """The Evidence_Fuser contribution of the best matching Indicator, or None."""
        hits = self.matches(ev)
        if not hits:
            return None
        return Contribution(CONTRIBUTOR_ID, ContributorType.INDICATOR,
                            indicator_mass(hits[0].confidence, self.cfg.base_mass), self.weight())

    def result(self, ev: Any, hits: Optional[Sequence[Indicator]] = None) -> Optional[SensorResult]:
        """Evidence_Chain record of the match (features: value, type, confidence, ...)."""
        hits = self.matches(ev) if hits is None else hits
        if not hits:
            return None
        best = hits[0]
        feats = {"value": best.value, "type": best.type.value, "confidence": float(best.confidence),
                 "source": best.source, "tlp": best.tlp.value, "matched": len(hits)}
        tx = str(getattr(ev, "tx_id", None) or getattr(ev, "event_id", ""))
        return SensorResult(tx, CONTRIBUTOR_ID, SensorOutcome.SUCCESS,
                            SensorOutput(indicator_mass(best.confidence, self.cfg.base_mass), feats, None), 0.0)

    def extend_stage1(self, ev: Any, stage1: FusedAssessment, results: Sequence[SensorResult], *,
                      high_conflict: float = 0.95) -> tuple[FusedAssessment, list[SensorResult]]:
        """Stage-1 assessment with the Indicator mass fused in (same shape as
        ``RuleEngine.extend_stage1``). Unchanged when nothing matches."""
        if not self._by_key:
            return stage1, list(results)
        hits = self.matches(ev)
        if not hits:
            return stage1, list(results)
        self.matches_total += 1
        c = Contribution(CONTRIBUTOR_ID, ContributorType.INDICATOR,
                         indicator_mass(hits[0].confidence, self.cfg.base_mass), self.weight())
        fa, _ = fuse([*stage1.contributions, c], high_conflict, tx_id=stage1.tx_id, stage=1)
        res = self.result(ev, hits)
        return fa, [*results, *([res] if res is not None else [])]

    # -- sharing (Req 34.5-34.8) ----------------------------------------------------------------

    def export_bundle(self, include_red: bool = False) -> dict[str, Any]:
        return to_bundle(self.all(), include_red=include_red)

    def import_bundle(self, doc: Any) -> ImportReport:
        """Parse and store every valid Indicator object; raises ``BundleError`` for a document
        that is not a bundle (nothing imported)."""
        rep = parse_bundle(doc)
        with self._lock:
            for i in rep.imported:
                self._by_key[i.key] = i
        self._persist(list(rep.imported))
        return rep

    def summary(self, at: Optional[datetime] = None) -> dict[str, Any]:
        now = at if at is not None else self.clock()
        inds = self.all()
        return {"count": len(inds), "expired": sum(1 for i in inds if i.expired_at(now)),
                "by_type": {t.value: sum(1 for i in inds if i.type is t) for t in IndicatorType},
                "matches_total": self.matches_total, "storage_errors": self.storage_errors,
                "config": self.cfg.to_dict()}


def helix_resolver(helix: Any) -> Callable[[str], Any]:
    """tx_id -> Transaction_Event from the pipeline's recent-event cache (duck-typed)."""

    def resolve(tx_id: str) -> Any:
        info_of = getattr(helix, "_info", None)
        info = info_of(tx_id) if callable(info_of) else None
        return getattr(info, "ev", None)

    return resolve


def attach_indicators(helix: Any, store: IndicatorStore) -> IndicatorStore:
    """Wire ``store`` to a ``Helix``: event resolver, stream-time clock, learned weights, the
    CONFIRMED_FRAUD feedback hook (idempotent) and ``helix.indicators``. Fusing the Indicator mass
    into stage 1 needs one call in the pipeline: ``store.extend_stage1(ev, stage1, results)``."""
    if store.resolver is None:
        store.resolver = helix_resolver(helix)
    clock = getattr(getattr(helix, "clock", None), "now", None)
    if callable(clock):
        store.clock = clock
    other = getattr(getattr(helix, "learner", None), "other_weights", None)
    if other is not None:
        store.weights = other
    hooks = getattr(helix, "feedback_hooks", None)
    if hooks is not None and not any(getattr(h, "indicator_store", None) is store for h in hooks):
        hooks.append(store.feedback_hook())
    helix.indicators = store
    return store


__all__ = [
    "CONTRIBUTOR_ID",
    "PATHS",
    "TLP",
    "TLP_MARKING_IDS",
    "BundleError",
    "DuckDBIndicatorBackend",
    "ImportReport",
    "Indicator",
    "IndicatorConfig",
    "IndicatorError",
    "IndicatorStore",
    "IndicatorType",
    "RejectedObject",
    "attach_indicators",
    "event_keys",
    "helix_resolver",
    "indicator_mass",
    "indicator_problems",
    "normalize_ip",
    "parse_bundle",
    "parse_indicator_object",
    "parse_pattern",
    "stix_pattern",
    "to_bundle",
    "to_stix",
]
