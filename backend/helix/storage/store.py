"""DuckDB Storage (design §20, Req 24): batched writer, snapshots, restore, graph rebuild.

Threading: every use of the DuckDB connection holds ``_lock``; flushes are serialised by
``_flush_lock``. The async ``writer()`` runs ``flush()`` through ``asyncio.to_thread``;
``flush()`` can also be called synchronously (tests, shutdown).

Rows are validated and serialised when they are enqueued, so a bad row raises at the
caller instead of poisoning a batch. Upsert tables replace by primary key; append-only
tables (``case_history``, ``weight_history``, ``config_changes``, ``rejected_events``)
use ``INSERT OR IGNORE``, so an earlier record is never changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

import duckdb

from helix.codec import TransactionEvent
from helix.fusion import Contribution, ContributorType, FusedAssessment, Mass
from helix.graph import TemporalGraph

from . import schema as S
from .serde import decode_json, encode_json

log = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 0.5
BATCH_ROWS = 1_000
MAX_CONSECUTIVE_FAILURES = 3
BUFFER_CAP = 1_000_000
BACKOFF_MAX_S = 10.0
SNAPSHOT_MIN_S, SNAPSHOT_MAX_S = 5.0, 3_600.0
_CENT = Decimal("0.01")
_AMOUNT_MIN, _AMOUNT_MAX = Decimal("0.01"), Decimal("999999999.99")

#: Returns ``(dna_state, cpd_state, ts)`` for a snapshot; called on the event loop.
SnapshotSource = Callable[[], "tuple[Mapping[str, Any], Mapping[str, Any], datetime]"]


# ---- errors / results -----------------------------------------------------------------
class StorageError(RuntimeError):
    pass


class StorageOpenError(StorageError):
    """The file cannot be opened; HELIX must refuse to start (Req 24.9). File untouched."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"cannot use storage file {path}: {reason}")
        self.path = path
        self.reason = reason


class SchemaMismatchError(StorageOpenError):
    pass


@dataclass(frozen=True, slots=True)
class StorageStatus:
    degraded: bool  # 3+ consecutive failed flushes; cleared by the next success
    consecutive_failures: int
    buffered: int  # rows waiting to be written
    dropped: int  # rows dropped because the buffer cap was exceeded
    flushed_rows: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class StoredEvent:
    seq_no: int
    event: TransactionEvent
    late: bool
    lateness_s: float
    received_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoredAssessment:
    assessment: FusedAssessment
    risk: float | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_no: int
    version: int
    ts: datetime
    dna: Any  # decoded DNA state (see serde.profile_store_to_dict)
    cpd: Any  # decoded CPD state (ChangePointDetector.to_dict)


@dataclass(frozen=True, slots=True)
class RestoredState:
    """Everything Storage restores at startup (Req 24.6). Rows are the stored payload
    dicts, ordered by primary key."""

    cases: list[dict]
    evidence_chains: list[dict]
    case_history: list[dict]
    feedback: list[dict]
    weight_history: list[dict]
    antibodies: list[dict]
    motif_instances: list[dict]
    config_changes: list[dict]
    snapshot: Snapshot | None
    latest_event_ts: datetime | None
    last_seq_no: int | None


# ---- conversion helpers ---------------------------------------------------------------
def _db_ts(dt: datetime | None, what: str = "timestamp") -> datetime | None:
    """Aware datetime -> UTC-naive for a TIMESTAMP column."""
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        raise ValueError(f"{what} must be a datetime, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _py_ts(naive: datetime | None, offset_s: int = 0) -> datetime | None:
    if naive is None:
        return None
    utc = naive.replace(tzinfo=timezone.utc)
    return utc if offset_s == 0 else utc.astimezone(timezone(timedelta(seconds=offset_s)))


def _typed(value: Any, sql_type: str, col: str) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    if sql_type == "TIMESTAMP":
        return _db_ts(value, col)
    if sql_type == "BIGINT":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{col} must be an int, got {value!r}")
        return value
    if sql_type == "BOOLEAN":
        if not isinstance(value, bool):
            raise ValueError(f"{col} must be a bool, got {value!r}")
        return value
    if not isinstance(value, str):
        raise ValueError(f"{col} must be a str, got {value!r}")
    return value


class Storage:
    def __init__(
        self,
        path: str | Path = "helix.duckdb",
        *,
        flush_interval_s: float = FLUSH_INTERVAL_S,
        batch_rows: int = BATCH_ROWS,
        max_failures: int = MAX_CONSECUTIVE_FAILURES,
        buffer_cap: int = BUFFER_CAP,
        backoff_max_s: float = BACKOFF_MAX_S,
        snapshot_interval_s: float = 60.0,
    ) -> None:
        if not SNAPSHOT_MIN_S <= snapshot_interval_s <= SNAPSHOT_MAX_S:
            raise ValueError(
                f"storage.snapshot_interval_s={snapshot_interval_s!r} is outside "
                f"[{SNAPSHOT_MIN_S:g}, {SNAPSHOT_MAX_S:g}]"
            )
        if batch_rows < 1 or buffer_cap < batch_rows or max_failures < 1 or flush_interval_s <= 0:
            raise ValueError("invalid Storage writer settings")
        self.path = Path(path)
        self.flush_interval_s = float(flush_interval_s)
        self.batch_rows = int(batch_rows)
        self.max_failures = int(max_failures)
        self.buffer_cap = int(buffer_cap)
        self.backoff_max_s = float(backoff_max_s)
        self.snapshot_interval_s = float(snapshot_interval_s)
        self.created = False  # True when open() created a new file

        self._con: Optional[duckdb.DuckDBPyConnection] = None
        self._lock = threading.RLock()  # connection
        self._flush_lock = threading.Lock()
        self._plock = threading.Lock()  # pending buffer
        self._pending: list[tuple[str, tuple]] = []
        self._counters: dict[str, int] = {}
        self._failures = 0
        self._degraded = False
        self._dropped = 0
        self._flushed = 0
        self._last_error: str | None = None
        self._cap_warned = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._stop: Optional[asyncio.Event] = None
        self._stopping = False

    @classmethod
    def from_config(cls, cfg: Any, **kw: Any) -> Storage:
        """Build from a ``HelixConfig`` or ``StorageConfig`` (path, snapshot_interval_s)."""
        sc = getattr(cfg, "storage", cfg)
        return cls(sc.path, snapshot_interval_s=sc.snapshot_interval_s, **kw)

    # ---- open / close -----------------------------------------------------------------
    def open(self, path: str | Path | None = None) -> Storage:
        """Open or create the file. Raises StorageOpenError / SchemaMismatchError without
        modifying an existing file (Req 24.8, 24.9)."""
        if self._con is not None:
            raise StorageError("storage is already open")
        if path is not None:
            self.path = Path(path)
        p = self.path
        if p.exists():
            self._check_existing(p)
            try:
                con = duckdb.connect(str(p))
            except duckdb.Error as exc:
                raise StorageOpenError(p, f"cannot open for writing: {exc}") from exc
            self.created = False
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            con = duckdb.connect(str(p))
            try:
                con.begin()
                for stmt in S.all_ddl():
                    con.execute(stmt)
                con.execute(
                    "INSERT INTO schema_version VALUES (?, ?)",
                    [S.SCHEMA_VERSION, _db_ts(datetime.now(timezone.utc))],
                )
                con.commit()
            except Exception:
                con.close()
                raise
            self.created = True
        self._con = con
        self._init_counters()
        self._stopping = False
        return self

    @staticmethod
    def _check_existing(p: Path) -> None:
        try:
            ro = duckdb.connect(str(p), read_only=True)
        except duckdb.Error as exc:
            raise StorageOpenError(p, f"unreadable file: {exc}") from exc
        try:
            layout = S.column_layout(ro)
            if "schema_version" not in layout:
                raise SchemaMismatchError(p, "missing schema_version table")
            versions = [r[0] for r in ro.execute("SELECT version FROM schema_version").fetchall()]
            if versions != [S.SCHEMA_VERSION]:
                raise SchemaMismatchError(
                    p, f"schema version {versions} does not match expected [{S.SCHEMA_VERSION}]"
                )
            for table, cols in S.expected_layout().items():
                if layout.get(table) != cols:
                    raise SchemaMismatchError(p, f"table {table!r} does not match the expected schema")
        except duckdb.Error as exc:
            raise StorageOpenError(p, f"cannot read schema: {exc}") from exc
        finally:
            ro.close()

    def _init_counters(self) -> None:
        autos = {t.name: t.auto_key for t in S.JSON_TABLES.values() if t.auto_key}
        autos |= {"rejected_events": "reject_no", "snapshots": "snapshot_no"}
        with self._lock:
            for table, col in autos.items():
                (n,) = self._conn.execute(f"SELECT COALESCE(MAX({col}), 0) FROM {table}").fetchone()
                self._counters[table] = int(n)

    @property
    def is_open(self) -> bool:
        return self._con is not None

    @property
    def _conn(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            raise StorageError("storage is not open")
        return self._con

    @contextlib.contextmanager
    def _locked(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Connection under the lock. Internal: used by GroundTruthStore only."""
        with self._lock:
            yield self._conn

    def close(self, final_snapshot: SnapshotSource | None = None) -> bool:
        """Flush every pending row, optionally write a final snapshot, close the file.
        Returns False if rows remained unwritten."""
        if self._con is None:
            return True
        ok = self.flush()
        if not ok:
            log.error("storage close: %d rows could not be written", self.pending)
        if final_snapshot is not None:
            try:
                self.snapshot(*final_snapshot())
            except Exception:
                log.exception("final snapshot failed")
                ok = False
        with self._lock:
            self._con.close()
            self._con = None
        return ok

    def __enter__(self) -> Storage:
        if self._con is None:
            self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- status -----------------------------------------------------------------------
    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def pending(self) -> int:
        with self._plock:
            return len(self._pending)

    def status(self) -> StorageStatus:
        return StorageStatus(
            self._degraded, self._failures, self.pending, self._dropped, self._flushed, self._last_error
        )

    # ---- enqueue (non-blocking) --------------------------------------------------------
    def _next(self, table: str) -> int:
        self._counters[table] = self._counters.get(table, 0) + 1
        return self._counters[table]

    def _push(self, table: str, params: tuple) -> None:
        with self._plock:
            self._pending.append((table, params))
            n = len(self._pending)
            if n > self.buffer_cap:
                k = n - self.buffer_cap
                del self._pending[:k]
                self._dropped += k
                log.error("storage buffer cap %d exceeded: dropped %d oldest rows", self.buffer_cap, k)
            elif n >= 0.8 * self.buffer_cap and not self._cap_warned:
                self._cap_warned = True
                log.warning("storage buffer at %d of %d rows", n, self.buffer_cap)
            elif n < 0.5 * self.buffer_cap:
                self._cap_warned = False
        if n >= self.batch_rows:
            self._signal()

    def _signal(self) -> None:
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            wake.set()
        else:
            loop.call_soon_threadsafe(wake.set)

    def enqueue(self, table: str, row: Mapping[str, Any]) -> Any:
        """Queue a row for one of the JSON tables (upsert or append by table kind).
        Returns the row's primary key (an auto-assigned number for auto-key tables)."""
        spec = S.JSON_TABLES.get(table)
        if spec is None:
            raise ValueError(f"unknown table {table!r}; use record_event / record_assessment")
        self._conn  # must be open
        row = dict(row)
        if spec.auto_key and row.get(spec.auto_key) is None:
            row[spec.auto_key] = self._next(table)
        for k in spec.keys:
            if row.get(k) is None:
                raise ValueError(f"{table}: key column {k!r} is required")
        params = tuple(_typed(row.get(c), t, f"{table}.{c}") for c, t in spec.columns)
        payload = encode_json(row)
        self._push(table, (*params, payload))
        key = tuple(row[k] for k in spec.keys)
        return key[0] if len(key) == 1 else key

    def upsert(self, table: str, row: Mapping[str, Any]) -> Any:
        spec = S.JSON_TABLES.get(table)
        if spec is None or spec.append_only:
            raise ValueError(f"{table!r} is not an upsert table")
        return self.enqueue(table, row)

    def append(self, table: str, row: Mapping[str, Any]) -> Any:
        spec = S.JSON_TABLES.get(table)
        if spec is None or not spec.append_only:
            raise ValueError(f"{table!r} is not an append-only table")
        return self.enqueue(table, row)

    def record_event(
        self,
        ev: Any,
        *,
        seq_no: int,
        late: bool = False,
        lateness_s: float = 0.0,
        received_at: datetime | None = None,
    ) -> None:
        self._conn
        amount = Decimal(str(ev.amount))
        q = amount.quantize(_CENT)
        if q != amount or not _AMOUNT_MIN <= q <= _AMOUNT_MAX:
            raise ValueError(f"events.amount {ev.amount!r} is not a 2-decimal amount in range")
        off = ev.ts.utcoffset()
        if off is None:
            raise ValueError("events.ts must be timezone-aware")
        off_s = int(off.total_seconds())
        if off_s != off.total_seconds():
            raise ValueError("events.ts offset must be whole seconds")
        lat, lon = getattr(ev, "lat", None), getattr(ev, "lon", None)
        self._push(
            "events",
            (
                int(seq_no), ev.tx_id, _db_ts(ev.ts), off_s, ev.src, ev.dst, q, ev.currency,
                getattr(ev, "channel", None), getattr(ev, "mcc", None),
                None if lat is None else float(lat), None if lon is None else float(lon),
                getattr(ev, "device_id", None), getattr(ev, "session_id", None),
                bool(late), float(lateness_s), _db_ts(received_at, "received_at"),
            ),
        )  # fmt: skip

    def record_assessment(self, fa: FusedAssessment, *, risk: float | None = None) -> None:
        self._conn
        contribs = [
            [c.contributor_id, str(c.kind), c.mass.f, c.mass.l, c.mass.t, float(c.weight)]
            for c in fa.contributions
        ]
        m = fa.mass
        self._push(
            "assessments",
            (
                fa.tx_id, int(fa.stage), float(m.f), float(m.l), float(m.t), float(fa.belief),
                float(fa.plausibility), float(fa.uncertainty), float(fa.conflict),
                bool(fa.high_conflict), None if risk is None else float(risk), encode_json(contribs),
            ),
        )  # fmt: skip

    def record_rejected(self, record: Any, source: str = "ingest") -> int:
        """Queue a ``helix.ingest.RejectedRecord``. Returns its reject_no."""
        self._conn
        no = self._next("rejected_events")
        self._push(
            "rejected_events",
            (
                no, _db_ts(record.received_at, "received_at"), str(record.raw),
                [str(f) for f in record.fields], str(getattr(record, "reason", "")), source,
            ),
        )  # fmt: skip
        return no

    # ---- flush ------------------------------------------------------------------------
    def _sql_for(self, table: str) -> tuple[str, tuple[int, ...] | None]:
        """Insert SQL and, for upsert tables, the key positions used to dedupe a batch."""
        if table == "events":
            return S.EVENTS_INSERT, (1,)
        if table == "assessments":
            return S.ASSESSMENTS_INSERT, (0, 1)
        if table == "rejected_events":
            return S.REJECTED_INSERT, None
        spec = S.JSON_TABLES[table]
        if spec.append_only:
            return spec.insert_sql(), None
        names = spec.column_names
        return spec.insert_sql(), tuple(names.index(k) for k in spec.keys)

    def _write_batch(self, batch: Sequence[tuple[str, tuple]]) -> None:
        groups: dict[str, list[tuple]] = {}
        for table, params in batch:
            groups.setdefault(table, []).append(params)
        con = self._conn
        con.begin()
        try:
            for table, rows in groups.items():
                sql, key_idx = self._sql_for(table)
                if key_idx is not None:  # last write per key wins, one row per key
                    last: dict[tuple, tuple] = {}
                    for r in rows:
                        k = tuple(r[i] for i in key_idx)
                        last.pop(k, None)
                        last[k] = r
                    rows = list(last.values())
                con.executemany(sql, rows)
            con.commit()
        except BaseException:
            with contextlib.suppress(Exception):
                con.rollback()
            raise

    def flush(self) -> bool:
        """Write every pending row in one transaction. Returns True on success (or when
        nothing is pending). Failures keep the rows buffered; after ``max_failures``
        consecutive failures Storage is degraded until a flush succeeds (Req 24.10)."""
        with self._flush_lock:
            with self._plock:
                batch = list(self._pending)
                dropped_before = self._dropped
            if not batch:
                return True
            try:
                with self._lock:
                    self._write_batch(batch)
            except Exception as exc:  # noqa: BLE001 - any write failure is retried
                self._failures += 1
                self._last_error = f"{type(exc).__name__}: {exc}"
                if self._failures >= self.max_failures and not self._degraded:
                    self._degraded = True
                    log.error(
                        "storage degraded after %d failed flushes (%d rows buffered): %s",
                        self._failures, len(batch), self._last_error,
                    )  # fmt: skip
                else:
                    log.warning("storage flush failed (%d): %s", self._failures, self._last_error)
                return False
            with self._plock:
                # Rows dropped from the head meanwhile were part of this batch.
                del self._pending[: max(0, len(batch) - (self._dropped - dropped_before))]
            if self._degraded:
                log.warning("storage recovered; %d buffered rows written", len(batch))
            self._failures = 0
            self._degraded = False
            self._last_error = None
            self._flushed += len(batch)
            return True

    def _backoff_s(self) -> float:
        return min(self.backoff_max_s, self.flush_interval_s * 2 ** max(0, self._failures - 1))

    def _ensure_events(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._wake is None or self._stop is None:
            self._loop = loop
            self._wake = asyncio.Event()
            self._stop = asyncio.Event()
            if self._stopping:
                self._stop.set()

    async def writer(self) -> None:
        """Flush every ``flush_interval_s`` or as soon as ``batch_rows`` are pending,
        backing off after failures. Runs until ``stop()``; does not do the final flush."""
        self._ensure_events()
        assert self._wake is not None and self._stop is not None
        while not self._stopping:
            if self._failures:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), self._backoff_s())
            else:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self.flush_interval_s)
            self._wake.clear()
            if self._stopping:
                break
            if self.pending:
                await asyncio.to_thread(self.flush)

    async def snapshotter(self, source: SnapshotSource, interval_s: float | None = None) -> None:
        """Snapshot DNA + CPD state every ``snapshot_interval_s`` until ``stop()``."""
        interval = self.snapshot_interval_s if interval_s is None else float(interval_s)
        self._ensure_events()
        assert self._stop is not None
        while not self._stopping:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), interval)
            if self._stopping:
                break
            dna, cpd, ts = source()  # on the loop: consistent with the single worker
            try:
                await asyncio.to_thread(self.snapshot, dna, cpd, ts)
            except Exception:
                log.exception("periodic snapshot failed")

    def stop(self) -> None:
        """Ask ``writer()`` and ``snapshotter()`` to return."""
        self._stopping = True
        for ev in (self._wake, self._stop):
            if ev is not None and self._loop is not None and not self._loop.is_closed():
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is self._loop:
                    ev.set()
                else:
                    self._loop.call_soon_threadsafe(ev.set)

    async def shutdown(
        self, final_snapshot: SnapshotSource | None = None, tasks: Sequence[asyncio.Task] = ()
    ) -> bool:
        """Stop the background tasks, flush everything, write the final snapshot, close
        (Req 24.5). Returns False if anything could not be written."""
        self.stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        snap = final_snapshot() if final_snapshot is not None else None
        return await asyncio.to_thread(self.close, (lambda: snap) if snap is not None else None)

    # ---- snapshots --------------------------------------------------------------------
    def snapshot(self, dna: Mapping[str, Any], cpd: Mapping[str, Any], ts: datetime) -> int:
        """Write DNA and CPD state as versioned JSON blobs in one transaction (sync).
        Returns the snapshot number."""
        db_ts = _db_ts(ts, "snapshot ts")
        blobs = [
            (kind, encode_json({"version": S.SNAPSHOT_VERSION, "kind": kind, "ts": ts, "state": st}).encode())
            for kind, st in (("DNA", dna), ("CPD", cpd))
        ]
        with self._lock:
            con = self._conn
            no = self._counters.get("snapshots", 0) + 1
            con.begin()
            try:
                con.executemany(
                    "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                    [(no, kind, S.SNAPSHOT_VERSION, db_ts, blob) for kind, blob in blobs],
                )
                con.commit()
            except BaseException:
                with contextlib.suppress(Exception):
                    con.rollback()
                raise
            self._counters["snapshots"] = no
        return no

    def latest_snapshot(self, at_or_before: datetime | None = None) -> Snapshot | None:
        with self._lock:
            con = self._conn
            if at_or_before is None:
                (no,) = con.execute("SELECT MAX(snapshot_no) FROM snapshots").fetchone()
            else:
                (no,) = con.execute(
                    "SELECT MAX(snapshot_no) FROM snapshots WHERE ts <= ?", [_db_ts(at_or_before)]
                ).fetchone()
            if no is None:
                return None
            rows = con.execute(
                "SELECT kind, version, blob FROM snapshots WHERE snapshot_no = ?", [no]
            ).fetchall()
        parts: dict[str, Any] = {}
        ts: datetime | None = None
        for kind, version, blob in rows:
            doc = decode_json(bytes(blob))
            if version != S.SNAPSHOT_VERSION or doc.get("version") != S.SNAPSHOT_VERSION:
                raise StorageError(f"snapshot {no} {kind} has unsupported version {version}")
            parts[kind] = doc["state"]
            ts = doc["ts"]
        if set(parts) != {"DNA", "CPD"} or ts is None:
            raise StorageError(f"snapshot {no} is incomplete: {sorted(parts)}")
        return Snapshot(int(no), S.SNAPSHOT_VERSION, ts, parts["DNA"], parts["CPD"])

    # ---- reads / restore --------------------------------------------------------------
    def load_rows(self, table: str) -> list[dict]:
        """Stored payload dicts of a JSON table, ordered by primary key."""
        spec = S.JSON_TABLES.get(table)
        if spec is None:
            raise ValueError(f"unknown table {table!r}")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM {spec.name} ORDER BY {', '.join(spec.keys)}"
            ).fetchall()
        return [decode_json(r[0]) for r in rows]

    def latest_event_ts(self) -> datetime | None:
        with self._lock:
            (ts,) = self._conn.execute("SELECT MAX(ts) FROM events").fetchone()
        return _py_ts(ts)

    def restore(self) -> RestoredState:
        """Load the persisted state (Req 24.6). Call before processing new events."""
        rows = {name: self.load_rows(name) for name in S.JSON_TABLES}
        with self._lock:
            ts, seq = self._conn.execute("SELECT MAX(ts), MAX(seq_no) FROM events").fetchone()
        return RestoredState(
            **rows,
            snapshot=self.latest_snapshot(),
            latest_event_ts=_py_ts(ts),
            last_seq_no=None if seq is None else int(seq),
        )

    def events(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> list[StoredEvent]:
        """Stored events with ``start <= ts <= end`` (bounds optional), ordered by (ts, tx_id)."""
        where, args = [], []
        if start is not None:
            where.append("ts >= ?")
            args.append(_db_ts(start))
        if end is not None:
            where.append("ts <= ?")
            args.append(_db_ts(end))
        sql = f"SELECT {', '.join(S.EVENT_COLUMNS)} FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts, tx_id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(zip(S.EVENT_COLUMNS, r))
            fields = {
                k: d[k]
                for k in ("tx_id", "src", "dst", "amount", "currency", "channel", "mcc", "lat", "lon", "device_id", "session_id")
                if d[k] is not None
            }
            fields["ts"] = _py_ts(d["ts"], d["ts_offset_s"])
            out.append(
                StoredEvent(
                    int(d["seq_no"]),
                    TransactionEvent(**fields),
                    bool(d["late"]),
                    float(d["lateness_s"]),
                    _py_ts(d["received_at"]),
                )
            )
        return out

    def assessments(self, tx_id: str | None = None) -> list[StoredAssessment]:
        sql = f"SELECT {', '.join(S.ASSESSMENT_COLUMNS)} FROM assessments"
        args: list[Any] = []
        if tx_id is not None:
            sql += " WHERE tx_id = ?"
            args.append(tx_id)
        sql += " ORDER BY tx_id, stage"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for tx, stage, f, l, t, bel, pl, unc, k, hc, risk, contribs in rows:  # noqa: E741
            cs = tuple(
                Contribution(cid, ContributorType(kind), Mass(cf, cl, ct), w)
                for cid, kind, cf, cl, ct, w in decode_json(contribs)
            )
            fa = FusedAssessment(tx, int(stage), Mass(f, l, t), bel, pl, unc, k, bool(hc), cs)
            out.append(StoredAssessment(fa, risk))
        return out

    def rejected_records(self) -> list[Any]:
        from helix.ingest.rejected_log import RejectedRecord

        with self._lock:
            rows = self._conn.execute(
                "SELECT received_at, raw, fields, reason FROM rejected_events ORDER BY reject_no"
            ).fetchall()
        return [RejectedRecord(r, _py_ts(ts), tuple(fs), reason) for ts, r, fs, reason in rows]

    def rebuild_graph(self, window_s: float, latest_ts: datetime | None = None) -> TemporalGraph:
        """Temporal_Graph from stored events in ``[latest_ts - window, latest_ts]``
        (Req 24.7). ``latest_ts`` defaults to the latest stored event timestamp."""
        g = TemporalGraph(window_s)
        if latest_ts is None:
            latest_ts = self.latest_event_ts()
            if latest_ts is None:
                return g
        for se in self.events(latest_ts - g.window, latest_ts):
            g.add(se.event)
        return g
