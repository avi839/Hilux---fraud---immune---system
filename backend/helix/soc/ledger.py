"""Tamper-evident Audit_Ledger (Req 35, 18.9): an append-only SHA-256 hash chain.

Entry (``LedgerEntry``)::

    index      0, 1, 2, ...                     position in the chain
    ts         "2024-06-01T12:00:00.000000Z"     UTC, microseconds, kept as the hashed string
    category   CONFIG_CHANGE | FEEDBACK_LABEL | SENSOR_WEIGHT_CHANGE | RULE_CHANGE |
               PLAYBOOK_ACTION | ESCALATION | WEIGHT_ROLLBACK      (Req 35.1)
    actor      who caused it ("investigator-7", "feedback_learner", "playbook_runner", ...)
    payload    JSON object (canonical form, see below)
    prev_hash  hash of the previous entry; 64 "0" characters for the first entry (Req 35.2)
    hash       sha256(canonical({index, ts, category, actor, payload, prev_hash})) hex

Design decisions:
- Canonical serialization (``canonical_json``): ``json.dumps`` with sorted keys, separators
  ``(",", ":")``, UTF-8 (no ASCII escaping) and ``allow_nan=False``. Payload values are first
  normalised (``normalize``): Decimal -> str, datetime -> ISO-8601 (aware ones in UTC with a
  ``Z``), date -> ISO, timedelta -> seconds, Enum -> value, dataclass -> dict, tuple -> list,
  set -> list sorted by canonical form. NaN / infinity, non-string keys that are not ints or
  enums, key collisions after conversion and unknown types are rejected (``LedgerError``)
  before anything is written. The stored payload is the canonical JSON text, so recomputing a
  hash from a stored entry reproduces the original bytes.
- Append-only (Req 35.1): the only mutation is ``append``; there is no update or delete API.
  ``entries()`` returns immutable ``LedgerEntry`` objects. With a DuckDB backend the entry and
  the head record are written in one transaction *before* the in-memory chain advances, so a
  storage fault raises and leaves the ledger unchanged.
- Head record (Req 35.3): ``HeadRecord(index, hash)`` of the latest entry; ``(-1, 64 zeros)``
  for an empty ledger. It is kept separately from the entries and is the verification anchor.
- Verification (Req 35.4-35.6, ``verify_chain``) walks the chain from index 0 and reports the
  first broken entry, choosing an index that is never after the first altered entry:

  * ``entry.index != position`` (deletion, reordering, insertion) -> ``min(position, index)``;
  * ``prev_hash`` differs from the previous entry's hash (an entry was re-hashed after an
    edit, or its link was edited) -> ``position - 1`` (0 for the first entry);
  * recomputed hash differs from the stored hash (content edited) -> ``position``;
  * the chain is internally consistent but does not end at the head record: when the head
    still matches an earlier entry ``k`` the entries after it are unanchored -> ``k + 1``;
    otherwise the alteration cannot be localised (truncation, or a rewritten suffix) -> 0.

  This is tamper-*evident*, not tamper-proof: someone who can rewrite the entries *and* the
  head record (same database) can produce a chain that verifies. Anchoring the head outside
  the database (printing it, a second store, a signature) is out of scope.
- Persistence is optional: ``DuckDBLedgerBackend`` owns the tables ``audit_ledger`` and
  ``audit_head``. It accepts either a ``helix.storage.Storage`` (it uses the connection under
  the Storage lock, duck-typed via ``_locked``) or a raw DuckDB connection. ``verify()``
  re-reads the stored rows when a backend is configured, so edits made directly in the
  database are detected.
- Integration (Req 18.9, 35.1): the small ``audit_*`` helpers and ``feedback_audit_hook`` /
  ``attach_audit`` let the integrator record config changes, Feedback_Labels, Sensor_Weight
  changes and rollbacks, Detection_Rule changes, escalations and Playbook_Action events
  without this module importing those components. The helpers never raise unless
  ``strict=True`` (an audit fault must not break a label request or the pipeline worker); a
  swallowed fault is logged and counted in ``AuditLedger.append_errors``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import math
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Protocol, Union

log = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64
MAX_ACTOR = 128
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_PAGE = 1_000
NOTICE = "tamper-evident, not tamper-proof"
CATEGORY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z$")


class AuditCategory(str, Enum):
    """Entry categories required by Req 35.1."""

    CONFIG_CHANGE = "CONFIG_CHANGE"
    FEEDBACK_LABEL = "FEEDBACK_LABEL"
    SENSOR_WEIGHT_CHANGE = "SENSOR_WEIGHT_CHANGE"
    RULE_CHANGE = "RULE_CHANGE"
    PLAYBOOK_ACTION = "PLAYBOOK_ACTION"
    ESCALATION = "ESCALATION"
    WEIGHT_ROLLBACK = "WEIGHT_ROLLBACK"

    def __str__(self) -> str:
        return self.value


class LedgerError(ValueError):
    """Invalid append (category, actor or payload); nothing was written."""


# --- canonical serialization --------------------------------------------------------------


def format_ts(ts: datetime) -> str:
    """Aware datetime -> ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (UTC)."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise LedgerError("timestamp must be timezone-aware")
    u = ts.astimezone(timezone.utc)  # explicit fields: strftime("%Y") is platform-dependent below 1000
    return (f"{u.year:04d}-{u.month:02d}-{u.day:02d}T{u.hour:02d}:{u.minute:02d}:{u.second:02d}"
            f".{u.microsecond:06d}Z")


def parse_ts(text: str) -> datetime:
    """Inverse of ``format_ts``; also accepts 0-9 fraction digits (truncated to microseconds)."""
    m = _TS.match(text) if isinstance(text, str) else None
    if m is None:
        raise ValueError(f"not a UTC timestamp of the form YYYY-MM-DDTHH:MM:SS[.f]Z: {text!r}")
    y, mo, d, h, mi, s, frac = m.groups()
    us = int((frac or "0")[:6].ljust(6, "0"))
    return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), us, tzinfo=timezone.utc)


def normalize(o: Any, path: str = "payload") -> Any:
    """JSON-compatible canonical value of ``o`` (see module docstring). Raises ``LedgerError``."""
    if isinstance(o, Enum):
        return normalize(o.value, path)
    if o is None or isinstance(o, (bool, str)):
        return o if not isinstance(o, str) else str(o)
    if isinstance(o, int):
        return int(o)
    if isinstance(o, float):
        if not math.isfinite(o):
            raise LedgerError(f"{path}: non-finite number {o!r}")
        return float(o)
    if isinstance(o, Decimal):
        if not o.is_finite():
            raise LedgerError(f"{path}: non-finite Decimal {o!r}")
        return str(o)
    if isinstance(o, datetime):
        return format_ts(o) if o.tzinfo is not None and o.utcoffset() is not None else o.isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, timedelta):
        return o.total_seconds()
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return normalize({f.name: getattr(o, f.name) for f in dataclasses.fields(o)}, path)
    if isinstance(o, Mapping):
        out: dict[str, Any] = {}
        for k, v in o.items():
            if isinstance(k, Enum):
                k = k.value
            if isinstance(k, bool) or not isinstance(k, (str, int)):
                raise LedgerError(f"{path}: unsupported key type {type(k).__name__}")
            ks = str(k)
            if ks in out:
                raise LedgerError(f"{path}: duplicate key {ks!r} after conversion")
            out[ks] = normalize(v, f"{path}.{ks}")
        return out
    if isinstance(o, (list, tuple)):
        return [normalize(v, f"{path}[{i}]") for i, v in enumerate(o)]
    if isinstance(o, (set, frozenset)):
        items = [normalize(v, f"{path}[]") for v in o]
        return sorted(items, key=canonical_json)
    raise LedgerError(f"{path}: unsupported type {type(o).__name__}")


def canonical_json(o: Any) -> str:
    """Canonical JSON text of an already-normalised value."""
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def entry_hash(index: int, ts: str, category: str, actor: str, payload: Any, prev_hash: str) -> str:
    """SHA-256 hex digest of the canonical serialization of one entry (Req 35.2)."""
    doc = {"index": index, "ts": ts, "category": category, "actor": actor, "payload": payload,
           "prev_hash": prev_hash}
    return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


# --- records ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    index: int
    ts: str  # canonical UTC timestamp string (hashed as is)
    category: str
    actor: str
    payload_json: str  # canonical JSON object
    prev_hash: str
    hash: str

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    @property
    def timestamp(self) -> datetime:
        return parse_ts(self.ts)

    def recompute_hash(self) -> Optional[str]:
        """Hash of the entry's content, or None when the stored content is not even decodable."""
        try:
            payload = json.loads(self.payload_json)
            return entry_hash(self.index, self.ts, self.category, self.actor, payload, self.prev_hash)
        except Exception:  # noqa: BLE001 - a tampered row must verify as broken, not crash
            return None

    def to_dict(self) -> dict[str, Any]:
        try:
            payload: Any = self.payload
        except ValueError:
            payload = self.payload_json
        return {"index": self.index, "ts": self.ts, "category": self.category, "actor": self.actor,
                "payload": payload, "prev_hash": self.prev_hash, "hash": self.hash}


@dataclass(frozen=True, slots=True)
class HeadRecord:
    index: int  # -1 for an empty ledger
    hash: str  # GENESIS_HASH for an empty ledger

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "hash": self.hash}


EMPTY_HEAD = HeadRecord(-1, GENESIS_HASH)


@dataclass(frozen=True, slots=True)
class VerifyResult:
    valid: bool
    first_broken_index: Optional[int]  # None when valid
    head: Optional[HeadRecord] = None
    entries: int = 0
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "first_broken_index": self.first_broken_index,
                "head": None if self.head is None else self.head.to_dict(), "entries": self.entries,
                "reason": self.reason, "notice": NOTICE}


def verify_chain(entries: Sequence[LedgerEntry], head: Optional[HeadRecord]) -> VerifyResult:
    """Recompute the hash chain of ``entries`` against ``head`` (Req 35.4-35.6)."""
    n = len(entries)

    def broken(i: int, why: str) -> VerifyResult:
        return VerifyResult(False, max(0, i), head, n, why)

    prev = GENESIS_HASH
    for p, e in enumerate(entries):
        idx = getattr(e, "index", None)
        if isinstance(idx, bool) or not isinstance(idx, int) or idx != p:
            at = min(p, idx) if isinstance(idx, int) and not isinstance(idx, bool) and idx >= 0 else p
            return broken(at, f"entry at position {p} carries index {idx!r}")
        if getattr(e, "prev_hash", None) != prev:
            return broken(p - 1, f"entry {p} does not link to the hash of entry {p - 1}" if p else
                          "entry 0 does not start from the genesis hash")
        if e.recompute_hash() != e.hash:
            return broken(p, f"entry {p} content does not match its hash")
        prev = e.hash
    if head is None:
        return broken(0, "no head record")
    if head.index == n - 1 and head.hash == prev:
        return VerifyResult(True, None, head, n, None)
    if 0 <= head.index < n - 1 and entries[head.index].hash == head.hash:
        return broken(head.index + 1, f"entries after index {head.index} are not anchored by the head record")
    return broken(0, "the chain does not end at the head record; the alteration cannot be localised")


# --- persistence -------------------------------------------------------------------------


class LedgerBackend(Protocol):
    def load(self) -> tuple[list[LedgerEntry], Optional[HeadRecord]]: ...

    def write(self, entry: LedgerEntry, head: HeadRecord) -> None: ...


@contextlib.contextmanager
def connection_scope(target: Any, lock: threading.RLock) -> Iterator[Any]:
    """DuckDB connection of ``target``: a ``helix.storage.Storage`` (its connection under its
    own lock, via ``_locked``) or a raw connection (under ``lock``)."""
    locked = getattr(target, "_locked", None)
    if callable(locked):
        with locked() as con:
            yield con
    else:
        with lock:
            yield target


@contextlib.contextmanager
def transaction(con: Any) -> Iterator[Any]:
    con.execute("BEGIN TRANSACTION")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def check_identifier(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"invalid table name {name!r}")
    return name


class DuckDBLedgerBackend:
    """Ledger rows in ``audit_ledger`` (insert-only) and the head in ``audit_head``."""

    def __init__(self, target: Any, entries_table: str = "audit_ledger", head_table: str = "audit_head") -> None:
        self.target = target
        self.entries_table = check_identifier(entries_table)
        self.head_table = check_identifier(head_table)
        self._lock = threading.RLock()
        with connection_scope(self.target, self._lock) as con:
            con.execute(
                f"CREATE TABLE IF NOT EXISTS {self.entries_table} (idx BIGINT PRIMARY KEY, ts VARCHAR NOT NULL, "
                "category VARCHAR NOT NULL, actor VARCHAR NOT NULL, payload VARCHAR NOT NULL, "
                "prev_hash VARCHAR NOT NULL, hash VARCHAR NOT NULL)")
            con.execute(f"CREATE TABLE IF NOT EXISTS {self.head_table} (id INTEGER PRIMARY KEY, "
                        "idx BIGINT NOT NULL, hash VARCHAR NOT NULL)")

    def load(self) -> tuple[list[LedgerEntry], Optional[HeadRecord]]:
        with connection_scope(self.target, self._lock) as con:
            rows = con.execute(f"SELECT idx, ts, category, actor, payload, prev_hash, hash FROM "
                               f"{self.entries_table} ORDER BY idx").fetchall()
            h = con.execute(f"SELECT idx, hash FROM {self.head_table} WHERE id = 1").fetchone()
        entries = [LedgerEntry(int(i), str(ts), str(c), str(a), str(p), str(ph), str(hh))
                   for i, ts, c, a, p, ph, hh in rows]
        return entries, (None if h is None else HeadRecord(int(h[0]), str(h[1])))

    def write(self, entry: LedgerEntry, head: HeadRecord) -> None:
        with connection_scope(self.target, self._lock) as con, transaction(con):
            con.execute(f"INSERT INTO {self.entries_table} VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [entry.index, entry.ts, entry.category, entry.actor, entry.payload_json,
                         entry.prev_hash, entry.hash])
            con.execute(f"INSERT OR REPLACE INTO {self.head_table} VALUES (1, ?, ?)", [head.index, head.hash])


# --- the ledger ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditLedger:
    """Append-only, hash-chained Audit_Ledger (Req 35)."""

    def __init__(self, *, clock: Callable[[], datetime] = _utcnow, backend: Optional[LedgerBackend] = None,
                 storage: Any = None, categories: Iterable[str] = ()) -> None:
        """``storage``: a ``Storage`` or DuckDB connection, shorthand for
        ``backend=DuckDBLedgerBackend(storage)``. ``categories``: extra accepted categories
        besides ``AuditCategory`` (upper-case identifiers)."""
        if backend is None and storage is not None:
            backend = DuckDBLedgerBackend(storage)
        self.clock = clock
        self.backend = backend
        extra = {self._category_name(c) for c in categories}
        self.categories: frozenset[str] = frozenset({c.value for c in AuditCategory} | extra)
        self.append_errors = 0  # faults swallowed by the non-strict audit_* helpers
        self._lock = threading.RLock()
        self._entries: list[LedgerEntry] = []
        self._head: HeadRecord = EMPTY_HEAD
        if backend is not None:
            entries, head = backend.load()
            self._entries = list(entries)
            if head is not None:
                self._head = head
            elif entries:
                self._head = HeadRecord(entries[-1].index, entries[-1].hash)

    @staticmethod
    def _category_name(category: Any) -> str:
        c = category.value if isinstance(category, Enum) else category
        if not isinstance(c, str) or not CATEGORY_PATTERN.match(c.strip().upper()):
            raise LedgerError(f"category must be an identifier like PLAYBOOK_ACTION, got {category!r}")
        return c.strip().upper()

    def append(self, category: str, actor: str, payload: Mapping[str, Any]) -> LedgerEntry:
        """Append one entry (Req 35.1, 35.2) and advance the head (Req 35.3). Raises
        ``LedgerError`` on an unknown category, an empty or over-long actor or a payload that is
        not a JSON-serialisable mapping; a backend fault propagates. Nothing changes on error."""
        cat = self._category_name(category)
        if cat not in self.categories:
            raise LedgerError(f"unknown audit category {cat!r}; expected one of {sorted(self.categories)}")
        if not isinstance(actor, str) or not actor.strip() or len(actor) > MAX_ACTOR:
            raise LedgerError(f"actor must be a non-empty string of at most {MAX_ACTOR} characters")
        if not isinstance(payload, Mapping):
            raise LedgerError("payload must be a mapping (JSON object)")
        body = normalize(payload)
        text = canonical_json(body)
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise LedgerError(f"payload larger than {MAX_PAYLOAD_BYTES} bytes")
        with self._lock:
            ts = format_ts(self.clock())
            index = len(self._entries)
            prev = self._entries[-1].hash if self._entries else GENESIS_HASH
            h = entry_hash(index, ts, cat, actor, body, prev)
            entry = LedgerEntry(index, ts, cat, actor, text, prev, h)
            head = HeadRecord(index, h)
            if self.backend is not None:
                self.backend.write(entry, head)  # storage first; a fault leaves the ledger unchanged
            self._entries.append(entry)
            self._head = head
            return entry

    def head(self) -> HeadRecord:
        return self._head

    def entries(self) -> tuple[LedgerEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def page(self, offset: int = 0, limit: int = 100, category: Optional[str] = None) -> tuple[int, list[LedgerEntry]]:
        """``(total matching, entries[offset:offset+limit])`` in index order."""
        if offset < 0 or not 1 <= limit <= MAX_PAGE:
            raise ValueError(f"offset must be >= 0 and limit in [1, {MAX_PAGE}]")
        rows: Sequence[LedgerEntry] = self.entries()
        if category is not None:
            cat = self._category_name(category)
            rows = [e for e in rows if e.category == cat]
        return len(rows), list(rows[offset:offset + limit])

    def verify(self) -> VerifyResult:
        """Recompute the chain against the head (Req 35.4). With a backend the stored rows are
        re-read, so edits made directly in the database are detected."""
        if self.backend is not None:
            entries, head = self.backend.load()
            return verify_chain(entries, head)
        with self._lock:
            return verify_chain(list(self._entries), self._head)


# --- integration helpers (Req 18.9, 35.1) ---------------------------------------------------


def audit_decision(ledger: Any, category: Union[str, AuditCategory], actor: str,
                   payload: Optional[Mapping[str, Any]] = None, *, strict: bool = False,
                   **fields: Any) -> Optional[LedgerEntry]:
    """Append ``{**payload, **fields}`` to ``ledger`` (anything with ``append(category, actor,
    payload)``). Returns the entry; ``None`` when ``ledger`` is None or (``strict=False``) the
    append failed, in which case the fault is logged and counted in ``ledger.append_errors``."""
    if ledger is None:
        return None
    doc = {**(payload or {}), **fields}
    cat = category.value if isinstance(category, Enum) else category
    try:
        return ledger.append(cat, actor, doc)
    except Exception:
        if strict:
            raise
        log.exception("audit: could not append %s by %s", cat, actor)
        with contextlib.suppress(Exception):
            ledger.append_errors = int(getattr(ledger, "append_errors", 0)) + 1
        return None


def _case_id(case: Any) -> str:
    return str(case if isinstance(case, str) else getattr(case, "case_id", case))


def audit_config_change(ledger: Any, actor: str, name: str, old: Any, new: Any, *, strict: bool = False,
                        **context: Any) -> Optional[LedgerEntry]:
    """HELIX_Config change of one setting (dotted path ``name``)."""
    return audit_decision(ledger, AuditCategory.CONFIG_CHANGE, actor, {"name": name, "old": old, "new": new},
                          strict=strict, **context)


def audit_setting_change(ledger: Any, change: Any, actor: str = "api", *, strict: bool = False) -> Optional[LedgerEntry]:
    """A Prioritiser ``SettingChange`` (``name, old, new, ts, period``) as a CONFIG_CHANGE."""
    return audit_config_change(ledger, actor, str(change.name), change.old, change.new, strict=strict,
                               at=getattr(change, "ts", None), period=getattr(change, "period", None))


def audit_feedback_label(ledger: Any, case: Any, label: Any, actor: Optional[str] = None, note: Optional[str] = None,
                         *, strict: bool = False) -> Optional[LedgerEntry]:
    """A stored Feedback_Label (Req 18.9). ``case`` is a Case (duck-typed) or a case id."""
    who = actor or getattr(case, "assignee", None) or "investigator"
    note = note if note is not None else getattr(case, "label_note", None)
    return audit_decision(ledger, AuditCategory.FEEDBACK_LABEL, str(who),
                          {"case_id": _case_id(case), "label": label, "note": note}, strict=strict)


def audit_escalation(ledger: Any, case: Any, actor: str, *, strict: bool = False, **context: Any) -> Optional[LedgerEntry]:
    """A Case escalation (Req 18.9)."""
    return audit_decision(ledger, AuditCategory.ESCALATION, actor,
                          {"case_id": _case_id(case), "tier": getattr(case, "tier", None)}, strict=strict, **context)


def audit_weight_record(ledger: Any, record: Any, actor: str = "feedback_learner", *,
                        strict: bool = False) -> Optional[LedgerEntry]:
    """One Feedback_Learner ``WeightRecord``: SENSOR_WEIGHT_CHANGE, or WEIGHT_ROLLBACK when the
    record carries ``rollback_to``."""
    rollback_to = getattr(record, "rollback_to", None)
    cat = AuditCategory.WEIGHT_ROLLBACK if rollback_to is not None else AuditCategory.SENSOR_WEIGHT_CHANGE
    return audit_decision(ledger, cat, actor, {
        "contributor_id": record.contributor_id, "kind": getattr(record, "kind", None), "old": record.old,
        "new": record.new, "at": getattr(record, "ts", None), "case_id": getattr(record, "case_id", None),
        "rollback_to": rollback_to}, strict=strict)


def audit_weight_records(ledger: Any, records: Iterable[Any], actor: str = "feedback_learner", *,
                         strict: bool = False) -> list[LedgerEntry]:
    out = [audit_weight_record(ledger, r, actor, strict=strict) for r in records]
    return [e for e in out if e is not None]


def audit_rollback(ledger: Any, actor: str, t: datetime, records: Sequence[Any], *,
                   strict: bool = False) -> Optional[LedgerEntry]:
    """One summary WEIGHT_ROLLBACK entry for a rollback request to time ``t``."""
    return audit_decision(ledger, AuditCategory.WEIGHT_ROLLBACK, actor, {
        "rollback_to": t, "changed": [{"contributor_id": r.contributor_id, "old": r.old, "new": r.new}
                                      for r in records]}, strict=strict)


def audit_rule_change(ledger: Any, actor: str, rule_id: str, action: str, version: Optional[int] = None, *,
                      strict: bool = False, **context: Any) -> Optional[LedgerEntry]:
    """Detection_Rule change: ``action`` is e.g. submitted / activated / deactivated."""
    return audit_decision(ledger, AuditCategory.RULE_CHANGE, actor,
                          {"rule_id": rule_id, "action": action, "version": version}, strict=strict, **context)


def audit_playbook_action(ledger: Any, actor: str, payload: Mapping[str, Any], *,
                          strict: bool = False) -> Optional[LedgerEntry]:
    return audit_decision(ledger, AuditCategory.PLAYBOOK_ACTION, actor, payload, strict=strict)


def feedback_audit_hook(ledger: Any) -> Callable[[Any, Any], None]:
    """A ``Helix.feedback_hooks`` callable ``(case, label)`` that records the Feedback_Label."""

    def hook(case: Any, label: Any) -> None:
        audit_feedback_label(ledger, case, label)

    hook.audit_ledger = ledger  # type: ignore[attr-defined]
    return hook


def weight_audit_hook(ledger: Any, prev: Optional[Callable[[Any], None]] = None) -> Callable[[Any], None]:
    """A ``FeedbackLearner.audit_hook`` (``helix.explain.audit.AuditRecord`` of a weight change)
    that appends SENSOR_WEIGHT_CHANGE, or WEIGHT_ROLLBACK for a rollback record (Req 19.8, 35.1),
    after calling ``prev`` (the hook it replaces). Never raises."""

    def hook(rec: Any) -> None:
        if prev is not None:
            try:
                prev(rec)
            except Exception:  # noqa: BLE001 - the ledger entry is still written
                log.exception("audit: previous weight hook failed")
        p = rec.prev if isinstance(rec.prev, Mapping) else {}
        n = rec.new if isinstance(rec.new, Mapping) else {}
        cat = (AuditCategory.WEIGHT_ROLLBACK if str(rec.action) == "weight_rollback"
               else AuditCategory.SENSOR_WEIGHT_CHANGE)
        audit_decision(ledger, cat, str(rec.actor or "feedback_learner"), {
            "contributor_id": n.get("contributor_id", p.get("contributor_id")), "kind": n.get("kind"),
            "old": p.get("weight"), "new": n.get("weight"), "at": rec.ts, "case_id": rec.case_id,
            "rollback_to": n.get("rollback_to")})

    hook.audit_ledger = ledger  # type: ignore[attr-defined]
    return hook


def attach_weight_audit(learner: Any, ledger: Any) -> Any:
    """Route ``learner``'s weight-change records into ``ledger`` (idempotent per ledger)."""
    cur = getattr(learner, "audit_hook", None)
    if learner is not None and getattr(cur, "audit_ledger", None) is not ledger:
        learner.audit_hook = weight_audit_hook(ledger, cur)
    return learner


def attach_audit(helix: Any, ledger: AuditLedger) -> AuditLedger:
    """Append the Feedback_Label hook to ``helix.feedback_hooks`` (idempotent per ledger) and
    expose the ledger as ``helix.ledger``. Escalations, weight changes, rule and config
    changes need explicit ``audit_*`` calls at their call sites."""
    hooks = getattr(helix, "feedback_hooks", None)
    if hooks is not None and not any(getattr(h, "audit_ledger", None) is ledger for h in hooks):
        hooks.append(feedback_audit_hook(ledger))
    helix.ledger = ledger
    return ledger


__all__ = [
    "EMPTY_HEAD",
    "GENESIS_HASH",
    "NOTICE",
    "AuditCategory",
    "AuditLedger",
    "DuckDBLedgerBackend",
    "HeadRecord",
    "LedgerBackend",
    "LedgerEntry",
    "LedgerError",
    "VerifyResult",
    "attach_audit",
    "attach_weight_audit",
    "weight_audit_hook",
    "audit_config_change",
    "audit_decision",
    "audit_escalation",
    "audit_feedback_label",
    "audit_playbook_action",
    "audit_rollback",
    "audit_rule_change",
    "audit_setting_change",
    "audit_weight_record",
    "audit_weight_records",
    "canonical_json",
    "connection_scope",
    "entry_hash",
    "feedback_audit_hook",
    "format_ts",
    "normalize",
    "parse_ts",
    "verify_chain",
]
