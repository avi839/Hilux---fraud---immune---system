"""Dataset_Loader for IBM AMLworld `*_Trans.csv` files (design §3a, Req 4).

Flow: `load()` validates the file, puts rows into `(ts, row)` order, applies the replay
scope, writes every in-scope label (and optional pattern membership) to the
Ground_Truth_Store, and keeps the in-scope events. `replay()` then submits those events
to the Stream_Ingestor, so every label precedes its event (Req 4.5).

Ordering: pass 1 streams the Timestamp column and stops at the first descent. If the
file is ordered, pass 2 streams it again. Otherwise rows are sorted stably by
`(ts, row)`: in memory for small files, or by DuckDB for large ones (it spills to disk).
`parse_row` is the single parser on every path.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import re
import tempfile
import time
from bisect import bisect_left
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Literal, Optional, Protocol, Union

from pydantic import ValidationError

from helix.codec import TransactionEvent
from helix.codec.models import AMOUNT_MAX, AMOUNT_MIN, CENT
from helix.config import ConfigError, ConfigValidationError
from helix.config.schema import DatasetConfig
from helix.sim.clock import SimClock
from helix.sim.ground_truth import GroundTruthRecord, GroundTruthWriter, GTSource

from .currencies import code_for, name_for
from .patterns import ParsedPatterns, PatternRow, parse_patterns

log = logging.getLogger("helix.datasets")

HEADER: tuple[str, ...] = (
    "Timestamp",
    "From Bank",
    "Account",
    "To Bank",
    "Account",
    "Amount Received",
    "Receiving Currency",
    "Amount Paid",
    "Payment Currency",
    "Payment Format",
    "Is Laundering",
)
N_COLS = len(HEADER)
AML_TS_FORMAT = "%Y/%m/%d %H:%M"
DEFAULT_SORT_IN_MEMORY_MAX_BYTES = 64 * 1024 * 1024
JITTER_BOUND_S = 0.1  # Req 4.4

_BANK = re.compile(r"^[0-9]+$", re.ASCII)
_OFFSET_TZ = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2}):?(\d{2})?$", re.IGNORECASE)

Speed = Union[float, Literal["max"]]
RowKey = tuple[datetime, str, str, Decimal, str, Optional[str]]


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class RowError:
    row: int
    reason: str


@dataclass(frozen=True, slots=True)
class LoaderError:
    path: Path
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


@dataclass(frozen=True, slots=True)
class ReplayScope:
    start: Optional[datetime] = None  # inclusive, aware
    end: Optional[datetime] = None  # exclusive, aware
    max_rows: Optional[int] = None  # None = no limit

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            t = getattr(self, name)
            if t is not None and (t.tzinfo is None or t.utcoffset() is None):
                raise ValueError(f"ReplayScope.{name} must be timezone-aware")
        if self.max_rows is not None and self.max_rows < 0:
            raise ValueError("ReplayScope.max_rows must be >= 0")

    @classmethod
    def from_config(cls, cfg: DatasetConfig) -> "ReplayScope":
        return cls(cfg.scope_start, cfg.scope_end, cfg.scope_max_rows)


SortMode = Literal["none", "memory", "duckdb"]


@dataclass(frozen=True, slots=True)
class LoadReport:
    path: Path
    parsed: int  # rows that parsed (in or out of scope)
    skipped: int  # rows that failed to parse
    out_of_scope: int  # parsed rows outside the scope window / over max_rows
    laundering: int  # in-scope rows labelled laundering
    sorted_externally: bool  # True when DuckDB sorted the rows
    first_ts: Optional[datetime]  # of in-scope rows
    last_ts: Optional[datetime]
    patterns: dict[str, int]  # pattern type -> patterns with >= 1 matched member
    pattern_rows_matched: int
    unmatched_pattern_rows: int
    in_scope: int = 0
    sort_mode: SortMode = "none"
    rows_read: int = 0  # data rows read before stopping
    stopped_early: bool = False  # True when reading stopped at scope.end / max_rows
    skip_reasons: dict[str, int] = field(default_factory=dict)
    # Filled in by replay()
    emitted: int = 0
    max_jitter_s: float = 0.0
    late_emissions: int = 0  # emissions more than 100 ms after schedule


class Ingest(Protocol):
    @property
    def backpressure(self) -> bool: ...

    def submit(self, raw: TransactionEvent) -> object: ...


@dataclass(slots=True)
class LoaderProgress:
    """Read-only live counters (design §24): updated by `load()` and `replay()`."""

    rows_read: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    in_scope: int = 0
    emitted: int = 0


# A parsed row: the event plus its label (True = FRAUD, False = LEGIT, None = no label).
ParsedRow = tuple[TransactionEvent, Optional[bool]]


class BoundRowCodec(Protocol):
    """A row codec bound to one file's header, stem and default time zone."""

    def ts_of(self, fields: list[str]) -> Optional[datetime]: ...

    def parse(self, fields: list[str], row: int) -> ParsedRow | RowError: ...


class RowCodec(Protocol):
    """Pluggable row parser of the Dataset_Loader (design §25): AMLworld or a Column_Mapping."""

    encoding: str
    delimiter: str

    def bind(self, header: Optional[list[str]], stem: str, tz: tzinfo) -> BoundRowCodec | str:
        """Check the header; return the bound codec or the header error."""
        ...


class _AmlBound:
    __slots__ = ("stem", "tz")

    def __init__(self, stem: str, tz: tzinfo) -> None:
        self.stem, self.tz = stem, tz

    def ts_of(self, fields: list[str]) -> Optional[datetime]:
        return parse_ts(fields[0], self.tz) if fields else None

    def parse(self, fields: list[str], row: int) -> ParsedRow | RowError:
        return parse_row(fields, row, self.stem, self.tz)


class AmlworldCodec:
    """The default codec: positional AMLworld columns, `parse_row` (Req 4.15, 4.22)."""

    encoding = "utf-8-sig"
    delimiter = ","

    def bind(self, header: Optional[list[str]], stem: str, tz: tzinfo) -> BoundRowCodec | str:
        bad = _check_header(header)
        return bad if bad is not None else _AmlBound(stem, tz)


# --------------------------------------------------------------------------- row codec


def resolve_tz(name: str) -> tzinfo:
    """`UTC`, a fixed offset such as `+05:30` / `UTC-3`, or an IANA zone name."""
    s = name.strip()
    if s.upper() in ("UTC", "Z", "GMT", "ETC/UTC"):
        return timezone.utc
    m = _OFFSET_TZ.match(s)
    if m is not None:
        sign = -1 if m.group(1) == "-" else 1
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        if delta >= timedelta(hours=24):
            raise ValueError(f"offset out of range: {name!r}")
        return timezone(sign * delta)
    from zoneinfo import ZoneInfo  # local import: needs tzdata on Windows

    return ZoneInfo(s)


def parse_ts(s: str, tz: tzinfo) -> Optional[datetime]:
    """`YYYY/MM/DD HH:MM` or ISO 8601 date-time; naive values are in `tz`. Returns UTC."""
    s = s.strip()
    if not s:
        return None
    try:
        dt = datetime.strptime(s, AML_TS_FORMAT)
    except ValueError:
        iso = s[:-1] + "+00:00" if s[-1] in "Zz" else s
        if len(iso) <= 10:  # a bare date is not a date-time
            return None
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def format_ts(ts: datetime, tz: tzinfo) -> str:
    """AMLworld form when it is exact, else ISO 8601 with an explicit offset."""
    local = ts.astimezone(tz)
    naive = local.replace(tzinfo=None)
    if local.second == 0 and local.microsecond == 0 and naive.replace(tzinfo=tz).astimezone(timezone.utc) == ts:
        return f"{naive.year:04d}/{naive.month:02d}/{naive.day:02d} {naive.hour:02d}:{naive.minute:02d}"
    return local.isoformat()


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _key_us(ts: datetime) -> int:
    d = ts - _EPOCH
    return (d.days * 86_400 + d.seconds) * 1_000_000 + d.microseconds


def _account(bank: str, acct: str) -> Optional[str]:
    bank, acct = bank.strip(), acct.strip()
    if not _BANK.match(bank) or not acct:
        return None
    return f"{int(bank)}:{acct}"


def parse_row(
    fields: list[str] | tuple[str, ...], row: int, stem: str, tz: tzinfo
) -> tuple[TransactionEvent, bool] | RowError:
    """One AMLworld row -> (event, is_laundering). Columns are read by position (Req 4.22)."""
    if len(fields) != N_COLS:
        return RowError(row, f"wrong_column_count:{len(fields)}")
    ts_s, fb, fa, tb, ta, _recv_amt, _recv_cur, paid, pay_cur, fmt, label = fields  # Req 4.15
    ts = parse_ts(ts_s, tz)
    if ts is None:
        return RowError(row, "bad_timestamp")
    src = _account(fb, fa)
    dst = _account(tb, ta)
    if src is None or dst is None:
        return RowError(row, "bad_account")
    try:
        amount = Decimal(paid.strip())
        if not amount.is_finite():
            return RowError(row, "bad_amount")
        amount = amount.quantize(CENT, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        return RowError(row, "bad_amount")
    if amount < AMOUNT_MIN:
        return RowError(row, "amount_below_min")
    if amount > AMOUNT_MAX:
        return RowError(row, "amount_above_max")
    currency = code_for(pay_cur)
    if currency is None:
        return RowError(row, "unknown_currency")
    label = label.strip()
    if label not in ("0", "1"):
        return RowError(row, "bad_label")
    channel = fmt.strip() or None
    try:
        ev = TransactionEvent(
            tx_id=f"aml-{stem}-{row}", ts=ts, src=src, dst=dst, amount=amount, currency=currency, channel=channel
        )
    except ValidationError as exc:
        names = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        return RowError(row, f"invalid_event:{','.join(names)}")
    return ev, label == "1"


def format_row(ev: TransactionEvent, laundering: bool, tz: tzinfo) -> list[str]:
    """Event -> AMLworld row in header order. Paid values are written to both amount columns."""
    fb, fa = ev.src.split(":", 1)
    tb, ta = ev.dst.split(":", 1)
    amount = str(ev.amount)
    cur = name_for(ev.currency)
    return [format_ts(ev.ts, tz), fb, fa, tb, ta, amount, cur, amount, cur, ev.channel or "", "1" if laundering else "0"]


def row_key(ev: TransactionEvent) -> RowKey:
    """Content key used to match pattern rows to loaded rows."""
    return (ev.ts, ev.src, ev.dst, ev.amount, ev.currency, ev.channel)


def _check_header(header: Optional[list[str]]) -> Optional[str]:
    if header is None:
        return "empty file: no header row"
    got = [h.strip().lower() for h in header]
    missing = [
        f"{i + 1}:{name}" for i, name in enumerate(HEADER) if i >= len(got) or got[i] != name.lower()
    ]
    if missing:
        return "missing or misplaced column(s): " + ", ".join(missing)
    if len(got) > N_COLS:
        return f"unexpected extra column(s) after column {N_COLS}"
    return None


# --------------------------------------------------------------------------- loader


class DatasetLoader:
    def __init__(
        self,
        cfg: DatasetConfig,
        ingest: Ingest,
        clock: SimClock,
        gt: GroundTruthWriter,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        sort_in_memory_max_bytes: int = DEFAULT_SORT_IN_MEMORY_MAX_BYTES,
        backpressure_poll_s: float = 0.005,
        codec: Optional[RowCodec] = None,
    ) -> None:
        self._codec: RowCodec = codec if codec is not None else AmlworldCodec()
        self.progress = LoaderProgress()
        self._cursor = 0
        self._cfg = cfg
        self._ingest = ingest
        self._clock = clock
        self._gt = gt
        self._sleep = sleep
        self._monotonic = monotonic
        self._mem_max = sort_in_memory_max_bytes
        self._poll = backpressure_poll_s
        self._events: Optional[list[TransactionEvent]] = None
        self._report: Optional[LoadReport] = None

    @property
    def events(self) -> tuple[TransactionEvent, ...]:
        return tuple(self._events or ())

    @property
    def remaining(self) -> int:
        """Loaded events not yet submitted by `replay()`."""
        return 0 if self._events is None else len(self._events) - self._cursor

    def count_before(self, ts: datetime) -> int:
        """Unsubmitted loaded events with timestamp < `ts` (they are in `(ts, row)` order)."""
        if self._events is None:
            return 0
        return bisect_left(self._events, ts, lo=self._cursor, key=lambda e: e.ts) - self._cursor

    # ------------------------------------------------------------------ load

    def load(
        self, path: Path | str, patterns_path: Path | str | None = None, scope: Optional[ReplayScope] = None
    ) -> LoadReport | LoaderError:
        path = Path(path)
        scope = scope if scope is not None else ReplayScope.from_config(self._cfg)
        try:
            tz = resolve_tz(self._cfg.timezone)
        except Exception as exc:  # ZoneInfoNotFoundError, ValueError
            return LoaderError(path, f"dataset.timezone={self._cfg.timezone!r} is not a known timezone ({exc})")

        pattern_lines: Optional[list[str]] = None
        if patterns_path is not None:
            patterns_path = Path(patterns_path)
            try:
                pattern_lines = patterns_path.read_text(encoding="utf-8-sig").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                return LoaderError(patterns_path, f"cannot read patterns file: {exc}")

        # Pass 1: header + order check. Reads the whole file when it is ordered, so decode
        # errors surface before any label is written.
        codec = self._codec
        try:
            with open(path, encoding=codec.encoding, newline="") as fh:
                reader = csv.reader(fh, delimiter=codec.delimiter)
                bound = codec.bind(next(reader, None), path.stem, tz)
                if isinstance(bound, str):
                    return LoaderError(path, bound)
                ordered = True
                prev: Optional[datetime] = None
                for fields in reader:
                    ts = bound.ts_of(fields)
                    if ts is None:
                        continue
                    if prev is not None and ts < prev:
                        ordered = False
                        break
                    prev = ts
            size = path.stat().st_size
        except FileNotFoundError:
            return LoaderError(path, "file not found")
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            return LoaderError(path, f"unreadable: {exc}")

        tmpdir: Optional[tempfile.TemporaryDirectory[str]] = None
        try:
            if ordered:
                mode: SortMode = "none"
                source: Iterator[tuple[int, list[str]]] = self._stream(path)
            elif size <= self._mem_max:
                mode = "memory"
                source = iter(self._sort_in_memory(path, bound))
            else:
                mode = "duckdb"
                tmpdir = tempfile.TemporaryDirectory(prefix="helix-aml-")
                source = self._sort_duckdb(path, bound, Path(tmpdir.name))
            # Materialise the in-scope rows before writing anything, so a decode error
            # mid-file still yields no labels (Req 4.11).
            rows = self._select(source, bound, scope)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            return LoaderError(path, f"unreadable: {exc}")
        finally:
            if tmpdir is not None:
                tmpdir.cleanup()

        events, flags, stats = rows
        now = self._clock.now()
        index: dict[RowKey, deque[str]] = {}
        for ev, laund in zip(events, flags):
            if laund is None:  # Column_Mapping label value in neither list: no label (Req 40.4)
                continue
            self._gt.write(
                GroundTruthRecord(
                    event_id=ev.tx_id, label="FRAUD" if laund else "LEGIT", source=GTSource.LOADER, written_at=now
                )
            )
            if laund:
                index.setdefault(row_key(ev), deque()).append(ev.tx_id)

        patterns: dict[str, int] = {}
        matched = unmatched = 0
        if pattern_lines is not None:
            assert patterns_path is not None
            patterns, matched, unmatched = self._apply_patterns(
                parse_patterns(pattern_lines), index, tz, Path(patterns_path).stem, now
            )

        self._events = events
        self._cursor = 0
        self.progress.in_scope = len(events)
        self._report = LoadReport(
            path=path,
            parsed=stats["parsed"],
            skipped=stats["skipped"],
            out_of_scope=stats["out_of_scope"],
            laundering=sum(1 for f in flags if f),
            sorted_externally=mode == "duckdb",
            first_ts=events[0].ts if events else None,
            last_ts=events[-1].ts if events else None,
            patterns=patterns,
            pattern_rows_matched=matched,
            unmatched_pattern_rows=unmatched,
            in_scope=len(events),
            sort_mode=mode,
            rows_read=stats["rows_read"],
            stopped_early=bool(stats["stopped_early"]),
            skip_reasons=dict(stats["reasons"]),
        )
        log.info(
            "loaded %s: parsed=%d skipped=%d out_of_scope=%d in_scope=%d unmatched_pattern_rows=%d",
            path, self._report.parsed, self._report.skipped, self._report.out_of_scope,
            self._report.in_scope, unmatched,
        )
        return self._report

    def _stream(self, path: Path) -> Iterator[tuple[int, list[str]]]:
        with open(path, encoding=self._codec.encoding, newline="") as fh:
            reader = csv.reader(fh, delimiter=self._codec.delimiter)
            next(reader, None)
            for row, fields in enumerate(reader, start=1):
                yield row, fields

    def _sort_in_memory(self, path: Path, bound: BoundRowCodec) -> list[tuple[int, list[str]]]:
        keyed: list[tuple[bool, datetime, int, list[str]]] = []
        for row, fields in self._stream(path):
            ts = bound.ts_of(fields)
            # Unparseable timestamps sort last (like DuckDB NULLS LAST); parse_row rejects them.
            keyed.append((ts is None, ts or _EPOCH, row, fields))
        keyed.sort(key=lambda t: (t[0], t[1], t[2]))
        return [(row, fields) for _, _, row, fields in keyed]

    def _sort_duckdb(self, path: Path, bound: BoundRowCodec, tmp: Path) -> Iterator[tuple[int, list[str]]]:
        """External sort: Python writes `(key_us, row, json(fields))`, DuckDB orders it.

        Row numbers come from `csv.reader`, so they match the streaming path exactly.
        """
        import duckdb

        keyed = tmp / "keyed.csv"
        with open(keyed, "w", encoding="utf-8", newline="") as out:
            w = csv.writer(out)
            for row, fields in self._stream(path):
                ts = bound.ts_of(fields)
                w.writerow(["" if ts is None else _key_us(ts), row, json.dumps(fields, ensure_ascii=False)])
        con = duckdb.connect(":memory:")
        try:
            con.execute("SET threads=1")
            con.execute("SET preserve_insertion_order=true")
            con.execute(f"SET temp_directory='{str(tmp).replace(chr(39), chr(39) * 2)}'")
            src = str(keyed).replace("'", "''")
            cur = con.execute(
                f"SELECT r, p FROM read_csv('{src}', header=false, auto_detect=false, delim=',', quote='\"', "
                "escape='\"', columns={'k': 'BIGINT', 'r': 'BIGINT', 'p': 'VARCHAR'}) "
                "ORDER BY k ASC NULLS LAST, r ASC"
            )
            while batch := cur.fetchmany(10_000):
                for r, p in batch:
                    yield int(r), json.loads(p)
        finally:
            con.close()

    def _select(
        self, source: Iterator[tuple[int, list[str]]], bound: BoundRowCodec, scope: ReplayScope
    ) -> tuple[list[TransactionEvent], list[Optional[bool]], dict]:
        """Parse rows in (ts, row) order and keep the in-scope ones (Property 76)."""
        events: list[TransactionEvent] = []
        flags: list[Optional[bool]] = []
        reasons: Counter[str] = Counter()
        parsed = skipped = out_of_scope = rows_read = 0
        stopped_early = False
        start, end, limit = scope.start, scope.end, scope.max_rows
        prog = self.progress
        prog.rows_read = prog.skipped = prog.in_scope = prog.emitted = 0
        prog.skip_reasons = {}
        for row, fields in source:
            rows_read += 1
            prog.rows_read = rows_read
            res = bound.parse(fields, row)
            if isinstance(res, RowError):
                log.warning("dataset row %d skipped: %s", res.row, res.reason)
                skipped += 1
                reasons[res.reason] += 1
                prog.skipped = skipped
                prog.skip_reasons[res.reason] = reasons[res.reason]
                continue
            ev, laund = res
            parsed += 1
            if start is not None and ev.ts < start:
                out_of_scope += 1
                continue
            if (end is not None and ev.ts >= end) or (limit is not None and len(events) >= limit):
                # Source is in ts order: every later valid row is out of scope too.
                out_of_scope += 1
                stopped_early = True
                break
            events.append(ev)
            flags.append(laund)
        close = getattr(source, "close", None)
        if close is not None:
            close()
        stats = dict(
            parsed=parsed, skipped=skipped, out_of_scope=out_of_scope, rows_read=rows_read,
            stopped_early=stopped_early, reasons=reasons,
        )
        return events, flags, stats

    def _apply_patterns(
        self, parsed: ParsedPatterns, index: dict[RowKey, deque[str]], tz: tzinfo, stem: str, now: datetime
    ) -> tuple[dict[str, int], int, int]:
        matched = unmatched = 0
        counts: Counter[str] = Counter()

        def miss(r: PatternRow, why: str) -> None:
            nonlocal unmatched
            unmatched += 1
            log.warning("pattern row (line %d) unmatched (%s): %s", r.lineno, why, ",".join(r.fields))

        for r in parsed.stray_rows:
            miss(r, "outside a BEGIN/END block")
        for block in parsed.blocks:
            pattern_id = f"{stem}-p{block.n}"
            hits = 0
            for r in block.rows:
                if not block.known:
                    miss(r, f"unknown pattern type {block.pattern_type}")
                    continue
                res = parse_row(r.fields, 0, "pattern", tz)
                if isinstance(res, RowError):
                    miss(r, res.reason)
                    continue
                dq = index.get(row_key(res[0]))
                if not dq:
                    miss(r, "no available loaded row with this content")
                    continue
                tx_id = dq.popleft()  # duplicate-content rows claim distinct txs in row order
                self._gt.write(
                    GroundTruthRecord(
                        event_id=tx_id, label="FRAUD", source=GTSource.LOADER, written_at=now,
                        pattern_id=pattern_id, pattern_type=block.pattern_type,
                    )
                )
                matched += 1
                hits += 1
            if hits:
                counts[block.pattern_type] += 1
        return dict(counts), matched, unmatched

    # ---------------------------------------------------------------- replay

    async def replay(self, speed: Optional[Speed] = None, count: Optional[int] = None) -> LoadReport:
        """Submit the loaded events. `"max"`: as fast as backpressure allows.

        A number: event i is due at `t0 + (ts_i - ts_0) / speed` on the monotonic clock,
        and the SimClock is moved to `ts_i` just before it is submitted.

        `count` submits only the next `count` unsubmitted events (Warm_Up_Mode, Req 40.7);
        a later call continues from there. Once every event is submitted the loader is spent.
        """
        speed = self._cfg.speed if speed is None else speed
        validate_speed(speed)
        if self._events is None or self._report is None:
            raise RuntimeError("DatasetLoader.replay() called before a successful load()")
        start = self._cursor
        stop = len(self._events) if count is None else min(len(self._events), start + max(0, count))
        events = self._events[start:stop]
        self._cursor = stop
        if stop >= len(self._events):
            self._events = None  # one-shot
        prog = self.progress
        emitted = late = 0
        max_jitter = 0.0
        if speed == "max":
            for ev in events:
                while self._ingest.backpressure:
                    await self._sleep(self._poll)
                self._ingest.submit(ev)
                emitted += 1
                prog.emitted += 1
                await self._sleep(0)  # let the pipeline worker run
        elif events:
            rate = float(speed)
            t0 = self._monotonic()
            ts0 = events[0].ts
            for ev in events:
                due = t0 + (ev.ts - ts0).total_seconds() / rate
                delay = due - self._monotonic()
                if delay > 0:
                    await self._sleep(delay)
                while self._ingest.backpressure:
                    await self._sleep(self._poll)
                jitter = max(0.0, self._monotonic() - due)
                max_jitter = max(max_jitter, jitter)
                if jitter > JITTER_BOUND_S:
                    late += 1
                now = self._clock.now()
                if ev.ts > now:
                    self._clock.advance(ev.ts - now)
                self._ingest.submit(ev)
                emitted += 1
                prog.emitted += 1
        if late:
            log.warning("AMLworld replay: %d emissions exceeded the %d ms jitter bound", late, JITTER_BOUND_S * 1000)
        r = self._report
        self._report = replace(
            r, emitted=r.emitted + emitted, max_jitter_s=max(r.max_jitter_s, max_jitter),
            late_emissions=r.late_emissions + late,
        )
        return self._report


def validate_speed(speed: object) -> None:
    """Req 4.12: "max" or a number in [1, 1_000_000]; raises naming `dataset.speed`."""
    if speed == "max" and isinstance(speed, str):
        return
    ok = (
        isinstance(speed, (int, float))
        and not isinstance(speed, bool)
        and math.isfinite(speed)
        and 1 <= speed <= 1_000_000
    )
    if not ok:
        raise ConfigValidationError([ConfigError("dataset.speed", speed, 'must be "max" or in [1, 1000000]')])
