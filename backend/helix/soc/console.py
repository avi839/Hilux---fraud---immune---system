"""SOC_Console (Req 36): SLA_Timers, analyst workload, shift handoff notes, Case age, MTTD / MTTR.

Design decisions:
- Pure read model over the Case_Manager. Cases are duck-typed (``helix.cases.Case`` fields:
  ``case_id``, ``seq_no``, ``status``, ``tier``, ``assignee``, ``created_at``, ``opened_at``,
  ``label_at``, ``merged_into``); nothing here mutates a Case.
- SLA_Timer (Req 36.1-36.3): two timers per open (OPEN or IN_REVIEW) Case, measured from Case
  creation with the targets of the Case's *current* Severity_Tier:
  ``time_to_acknowledge`` runs until the Case is first opened (IN_REVIEW, ``opened_at``) and is
  then stopped at that value; ``time_to_close`` runs until the Case is CLOSED. A timer is
  breached when its elapsed time exceeds its target; a stopped timer that breached stays
  breached (the Case was acknowledged late). Breached Cases carry the text label
  ``"SLA BREACHED"`` and the names of the breached timers, not only a colour.
- Time source: ``time_source="wall"`` (default, Req 36.2) reads ``datetime.now(UTC)``;
  ``"stream"`` reads the pipeline SimClock (useful for dataset replays whose timestamps are in
  the past, where wall-clock ages would be meaningless). Case timestamps are SimClock times; the
  API state starts that clock at wall time, so the two agree in a live run. Negative elapsed
  times (stream clock ahead of the wall clock) are clamped to 0.
- Workload (Req 36.4): IN_REVIEW Cases per self-declared Investigator name (the assignee).
- Case age (Req 36.6): ``now - created_at`` of each open Case.
- MTTD / MTTR (Req 36.7) over a window (default 24 h) ending at ``now``:
  MTTD = mean(created_at - first contributing event ts) over Cases created in the window, the
  first event ts being the earliest Evidence_Chain entry (``helix.chain(case_id)``);
  MTTR = mean(closed_at - created_at) over Cases closed by an Investigator label in the window
  (``label_at``). Cases closed by a merge (``merged_into``) are excluded from both: they were
  absorbed, not detected or responded to on their own.
- Handoff notes (Req 36.5): author (1-64 chars) + text (1-2000 chars) + timestamp, listed newest
  first. Kept in memory; with a Storage they are also written to the DuckDB table
  ``soc_handoff_notes`` (created here; the Storage facade is duck-typed through its
  connection context manager ``_locked()`` or a raw DuckDB connection) and reloaded on start.
  A failed write is logged and counted; the note stays in memory.
"""

from __future__ import annotations

import contextlib
import logging
import math
import statistics
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Optional, Union

from helix.config import SeverityTier

log = logging.getLogger(__name__)

ACK = "time_to_acknowledge"
CLOSE = "time_to_close"
TIMER_NAMES = (ACK, CLOSE)
BREACHED_LABEL = "SLA BREACHED"
DEFAULT_WINDOW_S = 24 * 3600.0
MAX_WINDOW_S = 90 * 86_400.0
MAX_AUTHOR_CHARS = 64
MAX_NOTE_CHARS = 2_000
TIME_SOURCES = ("wall", "stream")

_TIER_ORDER = (SeverityTier.CRITICAL, SeverityTier.HIGH, SeverityTier.MEDIUM, SeverityTier.LOW)


# --- DuckDB access (duck-typed Storage) ---------------------------------------------------------


@contextlib.contextmanager
def storage_connection(storage: Any) -> Iterator[Any]:
    """A DuckDB connection from ``storage``: the Storage facade's ``_locked()`` context
    manager (holds its connection lock) or, failing that, ``storage`` itself when it is a raw
    connection (has ``execute``)."""
    locked = getattr(storage, "_locked", None)
    if callable(locked):
        with locked() as con:
            yield con
    elif callable(getattr(storage, "execute", None)):
        yield storage
    else:
        raise TypeError(f"not a Storage or DuckDB connection: {type(storage).__name__}")


# --- SLA policy and timers ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlaTarget:
    ack_s: float  # time-to-acknowledge target, seconds
    close_s: float  # time-to-close target, seconds

    def to_dict(self) -> dict[str, float]:
        return {"ack_s": self.ack_s, "close_s": self.close_s}


DEFAULT_SLA_TARGETS: Mapping[SeverityTier, SlaTarget] = MappingProxyType({
    SeverityTier.CRITICAL: SlaTarget(15 * 60.0, 4 * 3600.0),
    SeverityTier.HIGH: SlaTarget(3600.0, 24 * 3600.0),
    SeverityTier.MEDIUM: SlaTarget(4 * 3600.0, 72 * 3600.0),
    SeverityTier.LOW: SlaTarget(24 * 3600.0, 7 * 86_400.0),
})


def _seconds(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
        raise ValueError(f"{what} must be a finite number of seconds > 0, got {v!r}")
    return float(v)


class SlaPolicy:
    """Per-Severity_Tier ack / close targets (Req 36.1); unspecified tiers keep the defaults."""

    def __init__(self, targets: Optional[Mapping[Union[SeverityTier, str], Any]] = None) -> None:
        out = dict(DEFAULT_SLA_TARGETS)
        for k, v in (targets or {}).items():
            try:
                tier = SeverityTier(getattr(k, "value", k))
            except ValueError:
                raise ValueError(f"unknown Severity_Tier {k!r}") from None
            if isinstance(v, SlaTarget):
                ack, close = v.ack_s, v.close_s
            elif isinstance(v, Mapping):
                ack, close = v.get("ack_s", out[tier].ack_s), v.get("close_s", out[tier].close_s)
            else:
                ack, close = v  # (ack_s, close_s)
            out[tier] = SlaTarget(_seconds(ack, f"{tier.value}.ack_s"), _seconds(close, f"{tier.value}.close_s"))
        self.targets: Mapping[SeverityTier, SlaTarget] = MappingProxyType(out)

    def target(self, tier: Any) -> SlaTarget:
        return self.targets[SeverityTier(getattr(tier, "value", tier))]

    def to_dict(self) -> dict[str, dict[str, float]]:
        return {t.value: self.targets[t].to_dict() for t in _TIER_ORDER}


@dataclass(frozen=True, slots=True)
class TimerState:
    name: str
    target_s: float
    elapsed_s: float
    running: bool  # False once the measured transition happened

    @property
    def breached(self) -> bool:
        return self.elapsed_s > self.target_s

    @property
    def remaining_s(self) -> float:
        return self.target_s - self.elapsed_s  # negative when breached

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "target_s": self.target_s, "elapsed_s": self.elapsed_s,
                "remaining_s": self.remaining_s, "running": self.running, "breached": self.breached}


@dataclass(frozen=True, slots=True)
class SlaTimer:
    case_id: str
    seq_no: int
    status: str
    tier: str
    assignee: Optional[str]
    created_at: datetime
    age_s: float  # Req 36.6
    ack: TimerState
    close: TimerState

    @property
    def breached_timers(self) -> tuple[str, ...]:
        return tuple(t.name for t in (self.ack, self.close) if t.breached)

    @property
    def breached(self) -> bool:
        return bool(self.breached_timers)

    @property
    def label(self) -> Optional[str]:
        return BREACHED_LABEL if self.breached else None

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "status": self.status, "tier": self.tier, "assignee": self.assignee,
                "created_at": self.created_at.isoformat(), "age_s": self.age_s, "breached": self.breached,
                "label": self.label, "breached_timers": list(self.breached_timers),
                "timers": {ACK: self.ack.to_dict(), CLOSE: self.close.to_dict()}}


def _value(x: Any) -> str:
    return str(getattr(x, "value", x))


def _elapsed(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds())


def closed_at(case: Any) -> Optional[datetime]:
    """When an Investigator closed the Case with a label; None for open or merge-closed Cases."""
    if _value(case.status) != "CLOSED" or getattr(case, "merged_into", None) is not None:
        return None
    return getattr(case, "label_at", None)


def sla_timer(case: Any, now: datetime, policy: Optional[SlaPolicy] = None) -> SlaTimer:
    """The SLA_Timer of one Case at ``now`` (Req 36.1-36.3, 36.6)."""
    pol = policy or SlaPolicy()
    tgt = pol.target(case.tier)
    created = case.created_at
    age = _elapsed(created, now)
    opened = getattr(case, "opened_at", None)
    end = closed_at(case)
    if _value(case.status) == "CLOSED" and end is None:  # merged: stopped at its last update
        end = getattr(case, "updated_at", None) or now
    ack = TimerState(ACK, tgt.ack_s, _elapsed(created, opened) if opened is not None else
                     (age if end is None else _elapsed(created, end)), opened is None and end is None)
    close = TimerState(CLOSE, tgt.close_s, age if end is None else _elapsed(created, end), end is None)
    return SlaTimer(case.case_id, int(getattr(case, "seq_no", 0)), _value(case.status), _value(case.tier),
                    getattr(case, "assignee", None), created, age, ack, close)


def is_open(case: Any) -> bool:
    return _value(case.status) != "CLOSED"


def sla_rows(cases: Iterable[Any], now: datetime, policy: Optional[SlaPolicy] = None) -> list[SlaTimer]:
    """SLA_Timers of the open Cases: breached first, then oldest first, then by Case id."""
    rows = [sla_timer(c, now, policy) for c in cases if is_open(c)]
    rows.sort(key=lambda r: (not r.breached, -r.age_s, r.seq_no, r.case_id))
    return rows


def workload(cases: Iterable[Any]) -> list[dict[str, Any]]:
    """IN_REVIEW Cases per Investigator name (Req 36.4), busiest first, then by name."""
    counts: dict[str, int] = {}
    for c in cases:
        if _value(c.status) == "IN_REVIEW" and getattr(c, "assignee", None):
            counts[c.assignee] = counts.get(c.assignee, 0) + 1
    return [{"investigator": k, "in_review": n} for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


@dataclass(frozen=True, slots=True)
class MeanTime:
    mean_s: Optional[float]  # None without samples
    cases: int

    def to_dict(self) -> dict[str, Any]:
        return {"mean_s": self.mean_s, "cases": self.cases}


def _mean(xs: list[float]) -> MeanTime:
    return MeanTime(statistics.fmean(xs) if xs else None, len(xs))


def response_metrics(cases: Iterable[Any], now: datetime, window_s: float = DEFAULT_WINDOW_S,
                     first_event_ts: Optional[Callable[[Any], Optional[datetime]]] = None
                     ) -> tuple[MeanTime, MeanTime]:
    """``(MTTD, MTTR)`` over ``(now - window_s, now]`` (Req 36.7)."""
    lo = now - timedelta(seconds=_seconds(window_s, "window_s"))
    detect: list[float] = []
    respond: list[float] = []
    for c in cases:
        if getattr(c, "merged_into", None) is not None:
            continue
        created = c.created_at
        if lo < created <= now and first_event_ts is not None:
            first = first_event_ts(c)
            if first is not None:
                detect.append(_elapsed(first, created))
        end = closed_at(c)
        if end is not None and lo < end <= now:
            respond.append(_elapsed(created, end))
    return _mean(detect), _mean(respond)


# --- handoff notes ------------------------------------------------------------------------------


class HandoffNoteError(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


@dataclass(frozen=True, slots=True)
class HandoffNote:
    note_id: int
    author: str
    ts: datetime
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"note_id": self.note_id, "author": self.author, "ts": self.ts.isoformat(), "text": self.text}


NOTES_TABLE = "soc_handoff_notes"
_NOTES_DDL = (f"CREATE TABLE IF NOT EXISTS {NOTES_TABLE} (note_id BIGINT PRIMARY KEY, author VARCHAR NOT NULL, "
              "ts VARCHAR NOT NULL, text VARCHAR NOT NULL)")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HandoffNoteStore:
    """Shift handoff notes (Req 36.5), newest first; optionally persisted to DuckDB."""

    def __init__(self, storage: Any = None, clock: Callable[[], datetime] = _utcnow) -> None:
        self.storage = storage
        self.clock = clock
        self.storage_errors = 0
        self._notes: list[HandoffNote] = []
        self._lock = threading.Lock()
        if storage is not None:
            self._load()

    def _load(self) -> None:
        try:
            with storage_connection(self.storage) as con:
                con.execute(_NOTES_DDL)
                rows = con.execute(f"SELECT note_id, author, ts, text FROM {NOTES_TABLE} ORDER BY note_id").fetchall()
        except Exception:  # noqa: BLE001 - notes must not stop the console
            self.storage_errors += 1
            log.exception("handoff notes: load failed; continuing in memory")
            return
        self._notes = [HandoffNote(int(i), str(a), datetime.fromisoformat(t), str(x)) for i, a, t, x in rows]

    @staticmethod
    def validate(author: Any, text: Any) -> tuple[str, str]:
        if not isinstance(author, str) or not author.strip():
            raise HandoffNoteError("author", "author must be a non-empty string")
        if len(author) > MAX_AUTHOR_CHARS:
            raise HandoffNoteError("author", f"author is longer than {MAX_AUTHOR_CHARS} characters")
        if not isinstance(text, str) or not text.strip():
            raise HandoffNoteError("text", "text must be a non-empty string")
        if len(text) > MAX_NOTE_CHARS:
            raise HandoffNoteError("text", f"text is {len(text)} characters; the maximum is {MAX_NOTE_CHARS}")
        return author.strip(), text

    def add(self, author: Any, text: Any, ts: Optional[datetime] = None) -> HandoffNote:
        a, t = self.validate(author, text)
        at = ts if ts is not None else self.clock()
        if at.tzinfo is None:
            raise HandoffNoteError("ts", "timestamp must be timezone-aware")
        with self._lock:
            note = HandoffNote((self._notes[-1].note_id if self._notes else 0) + 1, a, at, t)
            self._notes.append(note)
        if self.storage is not None:
            try:
                with storage_connection(self.storage) as con:
                    con.execute(_NOTES_DDL)
                    con.execute(f"INSERT INTO {NOTES_TABLE} VALUES (?, ?, ?, ?)",
                                [note.note_id, note.author, note.ts.isoformat(), note.text])
            except Exception:  # noqa: BLE001
                self.storage_errors += 1
                log.exception("handoff notes: write of note %d failed; kept in memory", note.note_id)
        return note

    def list(self, limit: Optional[int] = None) -> list[HandoffNote]:
        """Newest first (timestamp, then note id, descending)."""
        with self._lock:
            out = sorted(self._notes, key=lambda n: (n.ts, n.note_id), reverse=True)
        return out if limit is None else out[: max(0, int(limit))]

    def __len__(self) -> int:
        return len(self._notes)


# --- the console --------------------------------------------------------------------------------


def chain_first_event_ts(helix: Any) -> Callable[[Any], Optional[datetime]]:
    """First contributing event timestamp of a Case: its earliest Evidence_Chain entry."""

    def first(case: Any) -> Optional[datetime]:
        chain = helix.chain(case.case_id) if helix is not None else None
        entries = getattr(chain, "entries", None)
        if not entries:
            return None
        return min(e.ts for e in entries.values())

    return first


class SocConsole:
    """SOC_Console read model over a ``Helix`` pipeline (duck-typed: ``cases.cases``,
    ``chain(case_id)``, ``clock.now()``)."""

    def __init__(
        self,
        helix: Any,
        *,
        policy: Optional[SlaPolicy] = None,
        notes: Optional[HandoffNoteStore] = None,
        storage: Any = None,
        time_source: str = "wall",
        window_s: float = DEFAULT_WINDOW_S,
        wall_clock: Callable[[], datetime] = _utcnow,
        first_event_ts: Optional[Callable[[Any], Optional[datetime]]] = None,
    ) -> None:
        if time_source not in TIME_SOURCES:
            raise ValueError(f"time_source must be one of {TIME_SOURCES}, got {time_source!r}")
        self.helix = helix
        self.policy = policy or SlaPolicy()
        self.notes = notes if notes is not None else HandoffNoteStore(storage, wall_clock)
        self.time_source = time_source
        self.window_s = _seconds(window_s, "window_s")
        self.wall_clock = wall_clock
        self.first_event_ts = first_event_ts or chain_first_event_ts(helix)

    def cases(self) -> list[Any]:
        return list(self.helix.cases.cases.values())

    def now(self, time_source: Optional[str] = None) -> datetime:
        src = time_source or self.time_source
        if src not in TIME_SOURCES:
            raise ValueError(f"time_source must be one of {TIME_SOURCES}, got {src!r}")
        return self.helix.clock.now() if src == "stream" else self.wall_clock()

    def sla_rows(self, now: Optional[datetime] = None) -> list[SlaTimer]:
        return sla_rows(self.cases(), now or self.now(), self.policy)

    def workload(self) -> list[dict[str, Any]]:
        return workload(self.cases())

    def response_metrics(self, window_s: Optional[float] = None, now: Optional[datetime] = None
                         ) -> tuple[MeanTime, MeanTime]:
        return response_metrics(self.cases(), now or self.now(), self.window_s if window_s is None else window_s,
                                self.first_event_ts)

    def snapshot(self, *, window_s: Optional[float] = None, time_source: Optional[str] = None,
                 notes_limit: int = 20) -> dict[str, Any]:
        """The SOC_Console payload."""
        now = self.now(time_source)
        cases = self.cases()
        rows = sla_rows(cases, now, self.policy)
        w = self.window_s if window_s is None else _seconds(window_s, "window_s")
        mttd, mttr = response_metrics(cases, now, w, self.first_event_ts)
        return {
            "now": now.isoformat(),
            "time_source": time_source or self.time_source,
            "window_s": w,
            "sla_targets": self.policy.to_dict(),
            "open_cases": len(rows),
            "breached_cases": sum(1 for r in rows if r.breached),
            "cases": [r.to_dict() for r in rows],
            "workload": workload(cases),
            "mttd": mttd.to_dict(),
            "mttr": mttr.to_dict(),
            "handoff_notes": [n.to_dict() for n in self.notes.list(notes_limit)],
        }


__all__ = [
    "ACK",
    "BREACHED_LABEL",
    "CLOSE",
    "DEFAULT_SLA_TARGETS",
    "DEFAULT_WINDOW_S",
    "HandoffNote",
    "HandoffNoteError",
    "HandoffNoteStore",
    "MeanTime",
    "SlaPolicy",
    "SlaTarget",
    "SlaTimer",
    "SocConsole",
    "TimerState",
    "chain_first_event_ts",
    "closed_at",
    "response_metrics",
    "sla_rows",
    "sla_timer",
    "storage_connection",
    "workload",
]
