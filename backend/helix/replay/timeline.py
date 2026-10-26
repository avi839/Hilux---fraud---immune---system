"""Replay_Timeline, point-in-time frames and recompute (design §15, Req 17.4-17.8).

Everything here reads Storage; nothing is written and no HELIX state changes.

- ``replay_timeline``: span ``[earliest contributing event ts - lead, case.updated_at]``;
  every stored event with a Case account as source or destination in that span, ordered
  by ``(ts, tx_id)``, with its stored stage-1 assessment and sensor contributions.
- ``frame_at``: Related_Subgraph from stored edges with ts in ``[T - window, T]`` plus the
  timeline entries with ts <= T (Property 55).
- ``recompute``: restore the latest snapshot at or before the timeline start, build an
  engine through the factory and feed the logged events of the timeline's source
  accounts from the snapshot onward in ``seq_no`` order (Property 54).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Protocol

from helix.fusion import Contribution, ContributorType, FusedAssessment
from helix.storage import Snapshot, Storage, StoredEvent

from .engine import LAST_SEQ_KEY, EngineFactory
from .errors import ReplayParameterError, TimestampOutOfRangeError
from .subgraph import RelatedSubgraph, related_subgraph

LEAD_H_MIN, LEAD_H_MAX = 0.0, 168.0
_US = timedelta(microseconds=1)


class CaseLike(Protocol):
    case_id: str
    accounts: set[str]
    tx_ids: set[str]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    seq_no: int
    event: Any  # TransactionEvent
    late: bool
    assessment: FusedAssessment | None  # stored stage-1 Fused_Assessment
    stage2: FusedAssessment | None  # stored stage-2 assessment, when present
    risk: float | None  # stored risk of the stage-1 row

    @property
    def ts(self) -> datetime:
        return self.event.ts

    @property
    def tx_id(self) -> str:
        return self.event.tx_id

    @property
    def sensor_contributions(self) -> tuple[Contribution, ...]:
        if self.assessment is None:
            return ()
        return tuple(c for c in self.assessment.contributions if c.kind is ContributorType.SENSOR)


@dataclass(frozen=True, slots=True)
class ReplayTimeline:
    case_id: str
    start: datetime
    end: datetime
    entries: tuple[TimelineEntry, ...]

    def contains(self, t: datetime) -> bool:
        return self.start <= t <= self.end


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    case_id: str
    t: datetime
    window_start: datetime  # t - Graph_Window
    subgraph: RelatedSubgraph
    entries: tuple[TimelineEntry, ...]  # timeline entries with ts <= t

    @property
    def assessments(self) -> tuple[FusedAssessment, ...]:
        return tuple(e.assessment for e in self.entries if e.assessment is not None)


@dataclass(frozen=True, slots=True)
class ReplayRun:
    """Result of ``recompute``: one recomputed stage-1 assessment per timeline entry,
    in timeline order, plus what the run started from."""

    assessments: tuple[FusedAssessment, ...]
    snapshot_no: int | None  # None = started from empty state
    events_fed: int
    stored: tuple[FusedAssessment | None, ...] = field(default=())  # aligned with assessments


# ---- helpers --------------------------------------------------------------------------
def _utc(naive: datetime | None) -> datetime | None:
    return None if naive is None else naive.replace(tzinfo=timezone.utc)


def _scalar(storage: Storage, sql: str, args: list[Any]) -> Any:
    # Storage has no public aggregate reads yet; scalar queries only, decoding of event
    # rows stays in ``Storage.events``.
    with storage._locked() as con:  # noqa: SLF001
        return con.execute(sql, args).fetchone()[0]


def _placeholders(n: int) -> str:
    return ", ".join("?" for _ in range(n))


def earliest_event_ts(storage: Storage, tx_ids: Iterable[str]) -> datetime | None:
    ids = sorted(set(tx_ids))
    if not ids:
        return None
    return _utc(_scalar(storage, f"SELECT MIN(ts) FROM events WHERE tx_id IN ({_placeholders(len(ids))})", ids))


def check_lead_h(lead_h: float) -> float:
    if isinstance(lead_h, bool) or not isinstance(lead_h, (int, float)) or not LEAD_H_MIN <= lead_h <= LEAD_H_MAX:
        raise ReplayParameterError("replay.lead_h", lead_h, LEAD_H_MIN, LEAD_H_MAX)
    return float(lead_h)


def _stored_by_tx(storage: Storage, tx_id: str) -> tuple[Any, Any]:
    s1 = s2 = None
    for sa in storage.assessments(tx_id):
        if sa.assessment.stage == 1:
            s1 = sa
        elif sa.assessment.stage == 2:
            s2 = sa
    return s1, s2


# ---- timeline -------------------------------------------------------------------------
def replay_timeline(case: CaseLike, storage: Storage, lead_h: float = 24.0) -> ReplayTimeline:
    lead = timedelta(hours=check_lead_h(lead_h))
    end = case.updated_at
    first = earliest_event_ts(storage, case.tx_ids)
    if first is None:
        first = end  # no stored contributing event: empty-span timeline at the last update
    start = min(first - lead, end)
    accounts = case.accounts
    entries = []
    for se in storage.events(start, end):
        ev = se.event
        if ev.src not in accounts and ev.dst not in accounts:
            continue
        s1, s2 = _stored_by_tx(storage, ev.tx_id)
        entries.append(
            TimelineEntry(
                se.seq_no, ev, se.late,
                None if s1 is None else s1.assessment,
                None if s2 is None else s2.assessment,
                None if s1 is None else s1.risk,
            )
        )  # fmt: skip
    return ReplayTimeline(case.case_id, start, end, tuple(entries))


# ---- point in time --------------------------------------------------------------------
def frame_at(
    case: CaseLike,
    storage: Storage,
    t: datetime,
    *,
    window_s: float,
    hops: int = 2,
    max_nodes: int = 500,
    lead_h: float = 24.0,
    timeline: ReplayTimeline | None = None,
) -> ReplayFrame:
    """Related_Subgraph from stored edges in ``[t - window, t]`` and the timeline
    entries with ts <= t. Raises ``TimestampOutOfRangeError`` outside the timeline."""
    tl = timeline if timeline is not None else replay_timeline(case, storage, lead_h)
    if not tl.contains(t):
        raise TimestampOutOfRangeError(t, tl.start, tl.end)
    g = storage.rebuild_graph(window_s, latest_ts=t)
    lo = t - g.window
    sub = related_subgraph(case.accounts, g, hops, max_nodes, start=lo, end=t)
    return ReplayFrame(case.case_id, t, lo, sub, tuple(e for e in tl.entries if e.ts <= t))


# ---- weights --------------------------------------------------------------------------
def weight_records(storage: Storage) -> list[dict]:
    """WeightRecord rows ``{contributor_id, new, ts, ...}`` in record order."""
    return [r for r in storage.load_rows("weight_history") if "contributor_id" in r and "new" in r and "ts" in r]


def weights_at(records: Iterable[Mapping[str, Any]], t: datetime) -> dict[str, float]:
    """Sensor_Weights in effect at ``t``: the latest record per contributor with ts <= t."""
    w: dict[str, float] = {}
    for r in records:
        if r["ts"] <= t:
            w[r["contributor_id"]] = float(r["new"])
    return w


# ---- recompute ------------------------------------------------------------------------
def _resume_after(snap: Snapshot) -> tuple[int | None, datetime]:
    last = snap.dna.get(LAST_SEQ_KEY) if isinstance(snap.dna, Mapping) else None
    return (None if last is None else int(last)), snap.ts


def _covers(snap: Snapshot, entries: tuple[TimelineEntry, ...]) -> bool:
    """True when no timeline event is already folded into ``snap``."""
    last, ts = _resume_after(snap)
    if last is not None:
        return all(e.seq_no > last for e in entries)
    return all(e.ts > ts for e in entries)


def recompute(
    case: CaseLike,
    storage: Storage,
    factory: EngineFactory,
    cfg: Any,
    *,
    lead_h: float | None = None,
    timeline: ReplayTimeline | None = None,
) -> ReplayRun:
    """Recompute every timeline entry's stage-1 assessment from stored state.

    Events are fed after the snapshot's ``last_seq_no`` when the snapshot carries one,
    otherwise those with ts after the snapshot ts. Only events whose source is a
    source of some timeline event are fed: DNA, CPD state and sensors of an event
    depend only on its source account's history.
    """
    lead = cfg.replay.lead_h if lead_h is None else lead_h
    tl = timeline if timeline is not None else replay_timeline(case, storage, lead)
    entries = tl.entries
    if not entries:
        return ReplayRun((), None, 0, ())

    snap = storage.latest_snapshot(tl.start)
    while snap is not None and not _covers(snap, entries):
        snap = storage.latest_snapshot(snap.ts - _US)

    srcs = sorted({e.event.src for e in entries})
    max_seq = max(e.seq_no for e in entries)
    ph = _placeholders(len(srcs))
    if snap is None:
        last_seq, snap_ts = None, None
        lo = _utc(_scalar(storage, f"SELECT MIN(ts) FROM events WHERE src IN ({ph})", srcs))
    else:
        last_seq, snap_ts = _resume_after(snap)
        if last_seq is not None:
            lo = _utc(_scalar(
                storage, f"SELECT MIN(ts) FROM events WHERE seq_no > ? AND src IN ({ph})", [last_seq, *srcs]
            ))  # fmt: skip
        else:
            lo = snap_ts + _US
    src_set = set(srcs)

    def fed(se: StoredEvent) -> bool:
        if se.event.src not in src_set or se.seq_no > max_seq:
            return False
        if last_seq is not None:
            return se.seq_no > last_seq
        return snap_ts is None or se.event.ts > snap_ts

    stream = sorted((se for se in storage.events(lo) if fed(se)), key=lambda se: se.seq_no)

    records = weight_records(storage)
    # Weights in effect at the snapshot; later records are applied as they are passed.
    initial = weights_at(records, snap_ts) if snap_ts is not None else {}
    pending = sorted(
        (r for r in records if snap_ts is None or r["ts"] > snap_ts), key=lambda r: r["ts"]
    )
    engine = factory(snap, initial, cfg)
    set_weights = getattr(engine, "set_weights", None)
    current = dict(initial)

    out: dict[str, FusedAssessment] = {}
    i = 0
    for se in stream:
        changed = False
        while i < len(pending) and pending[i]["ts"] <= se.event.ts:
            current[pending[i]["contributor_id"]] = float(pending[i]["new"])
            i += 1
            changed = True
        if changed and set_weights is not None:
            set_weights(current)
        out[se.event.tx_id] = engine.process(se.event)

    return ReplayRun(
        tuple(out[e.tx_id] for e in entries),
        None if snap is None else snap.snapshot_no,
        len(stream),
        tuple(e.assessment for e in entries),
    )


def assessment_diff(a: FusedAssessment, b: FusedAssessment) -> float:
    """Largest absolute difference over masses, derived values, K and contributions;
    ``inf`` when they differ structurally (tx, stage, high_conflict, contributors)."""
    if (a.tx_id, a.stage, a.high_conflict) != (b.tx_id, b.stage, b.high_conflict):
        return float("inf")
    if len(a.contributions) != len(b.contributions):
        return float("inf")
    vals = [
        (a.mass.f, b.mass.f), (a.mass.l, b.mass.l), (a.mass.t, b.mass.t),
        (a.belief, b.belief), (a.plausibility, b.plausibility),
        (a.uncertainty, b.uncertainty), (a.conflict, b.conflict),
    ]  # fmt: skip
    for ca, cb in zip(a.contributions, b.contributions):
        if (ca.contributor_id, ca.kind) != (cb.contributor_id, cb.kind):
            return float("inf")
        vals += [(ca.mass.f, cb.mass.f), (ca.mass.l, cb.mass.l), (ca.mass.t, cb.mass.t), (ca.weight, cb.weight)]
    return max(abs(x - y) for x, y in vals)


def mismatches(run: ReplayRun, tol: float = 1e-9) -> list[str]:
    """tx_ids whose recomputed assessment differs from the stored one by more than tol
    (or that have no stored stage-1 assessment)."""
    bad = []
    for got, want in zip(run.assessments, run.stored):
        if want is None or assessment_diff(got, want) > tol:
            bad.append(got.tx_id)
    return bad


__all__ = [
    "LEAD_H_MAX",
    "LEAD_H_MIN",
    "CaseLike",
    "ReplayFrame",
    "ReplayRun",
    "ReplayTimeline",
    "TimelineEntry",
    "assessment_diff",
    "check_lead_h",
    "earliest_event_ts",
    "frame_at",
    "mismatches",
    "recompute",
    "replay_timeline",
    "weight_records",
    "weights_at",
]
