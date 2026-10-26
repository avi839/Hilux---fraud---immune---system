"""Dataset_Feeder: stream a dataset file into the live API_Server (design §24, §25; Req 39, 40).

The feeder owns lifecycle, status and progress only. Parsing, ordering, scope and label
writing stay in the Dataset_Loader (AMLworld codec, or a Column_Mapping through the
CSV_Adapter's ``MappingCodec``):

1. ``precheck()`` (before serving) opens every file and checks its header and the
   Column_Mapping; it raises ``DatasetStartupError(path, reason)`` (Req 39.9).
2. ``run()`` (background task "dataset"): LOADING -> ``loader.load`` in a thread, which writes
   every label and pattern membership to the Ground_Truth_Store before the first submit
   (Req 39.4) -> the SimClock jumps to the first in-scope timestamp -> optional WARMING_UP
   (``Helix.warmup`` set, rows at "max" speed, Req 40.7) -> STREAMING at ``speed`` -> DONE.
   The loader awaits ``not ingest.backpressure`` before each submit, so nothing is dropped
   and the order is ``(ts, row)`` (Req 39.2, 39.3). Any error sets FAILED; the server keeps
   running.

Ground-truth isolation: the feeder only holds the label *writer*; it never imports the
read side (``tests/property/test_ground_truth_isolation.py``).
"""

from __future__ import annotations

import asyncio
import csv
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional, Union

from .amlworld import DatasetLoader, LoaderError, LoaderProgress, LoadReport, ReplayScope, _check_header
from .csv_adapter import MappingCodec
from .mapping import ColumnMapping, header_errors, load_mapping
from .warmup import WarmupReport, build_report

log = logging.getLogger("helix.datasets.feed")

DEFAULT_MAX_ROWS = 20_000  # same default as scripts/aml_replay.py; 0 on the CLI = no limit
Speed = Union[float, Literal["max"]]


class FeedStatus(str, Enum):
    DISABLED = "DISABLED"
    LOADING = "LOADING"
    WARMING_UP = "WARMING_UP"
    STREAMING = "STREAMING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """From the CLI, validated before serving."""

    kind: Literal["amlworld", "csv"]
    trans: Path
    patterns: Optional[Path] = None
    mapping: Optional[Path] = None
    speed: Speed = "max"
    max_rows: Optional[int] = DEFAULT_MAX_ROWS  # None = no limit
    warmup: bool = False
    warmup_until: Optional[datetime] = None  # aware


class DatasetStartupError(Exception):
    def __init__(self, path: Union[Path, str], reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = Path(path)
        self.reason = reason


@dataclass(slots=True)
class FeedProgress:
    status: FeedStatus
    rows_read: int = 0
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    in_scope: int = 0
    submitted: int = 0
    warmup: Optional[WarmupReport] = None
    error: Optional[str] = None

    @property
    def fraction(self) -> Optional[float]:
        """Progress: submitted Transaction_Events over in-scope rows."""
        return None if self.in_scope <= 0 else self.submitted / self.in_scope


def _check_readable(path: Path, what: str) -> None:
    if not path.exists():
        raise DatasetStartupError(path, f"{what} file not found")
    if not path.is_file():
        raise DatasetStartupError(path, f"{what} path is not a file")
    try:
        with open(path, "rb") as fh:
            fh.read(1)
    except OSError as exc:
        raise DatasetStartupError(path, f"cannot read {what} file: {exc}") from exc


def _read_header(path: Path, encoding: str, delimiter: str) -> Optional[list[str]]:
    try:
        with open(path, encoding=encoding, newline="") as fh:
            return next(csv.reader(fh, delimiter=delimiter), None)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise DatasetStartupError(path, f"unreadable: {exc}") from exc


class DatasetFeeder:
    def __init__(
        self,
        spec: DatasetSpec,
        state: Any,  # helix.api.state.AppState (ingestor, clock, gt_writer, cfg, helix)
        loader: Optional[DatasetLoader] = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_s: float = 0.01,
        load_in_thread: bool = True,
    ) -> None:
        self.spec = spec
        self.state = state
        self.mapping: Optional[ColumnMapping] = None
        self._loader = loader
        self._sleep = sleep
        self._monotonic = monotonic
        self._poll = poll_s
        self._in_thread = load_in_thread
        self.status = FeedStatus.LOADING
        self.report: Optional[LoadReport] = None
        self.warmup_report: Optional[WarmupReport] = None
        self.error: Optional[str] = None

    # ------------------------------------------------------------------ startup

    def precheck(self) -> None:
        """Open every file and check headers and the Column_Mapping (Req 39.9, 40.1)."""
        s = self.spec
        if s.kind == "csv":
            if s.mapping is None:
                raise DatasetStartupError("--mapping", "required with --dataset csv")
            _check_readable(s.mapping, "mapping")
            m = load_mapping(s.mapping)
            if isinstance(m, list):
                raise DatasetStartupError(s.mapping, "invalid Column_Mapping: " + "; ".join(map(str, m)))
            self.mapping = m
        _check_readable(s.trans, "transactions")
        if s.kind == "csv":
            assert self.mapping is not None
            header = _read_header(s.trans, self.mapping.encoding, self.mapping.delimiter)
            errs = header_errors(self.mapping, header)
            if errs:
                raise DatasetStartupError(s.trans, "invalid header: " + "; ".join(map(str, errs)))
        else:
            bad = _check_header(_read_header(s.trans, "utf-8-sig", ","))
            if bad is not None:
                raise DatasetStartupError(s.trans, f"invalid header: {bad}")
        if s.patterns is not None:
            if s.kind != "amlworld":
                raise DatasetStartupError(s.patterns, "--patterns is accepted only with --dataset amlworld")
            _check_readable(s.patterns, "patterns")
            try:
                s.patterns.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                raise DatasetStartupError(s.patterns, f"unreadable: {exc}") from exc

    def build_loader(self) -> DatasetLoader:
        st = self.state
        codec = None
        if self.spec.kind == "csv":
            if self.mapping is None:
                self.precheck()
            assert self.mapping is not None
            codec = MappingCodec(self.mapping)
        return DatasetLoader(st.cfg.dataset, st.ingestor, st.clock, st.gt_writer, sleep=self._sleep,
                             monotonic=self._monotonic, codec=codec)

    @property
    def loader(self) -> Optional[DatasetLoader]:
        return self._loader

    # ------------------------------------------------------------------ run

    def _set_warmup(self, on: bool) -> None:
        helix = getattr(self.state, "helix", None)
        if helix is not None:
            helix.warmup = on

    async def _drain(self) -> None:
        """Wait until the pipeline worker has processed every submitted event."""
        ing = self.state.ingestor
        while len(ing) > 0:
            await self._sleep(self._poll)
        await asyncio.sleep(0)

    async def run(self) -> None:
        s = self.spec
        try:
            self.status = FeedStatus.LOADING
            loader = self._loader if self._loader is not None else self.build_loader()
            self._loader = loader
            base = ReplayScope.from_config(self.state.cfg.dataset)
            scope = ReplayScope(base.start, base.end, s.max_rows if s.max_rows else base.max_rows)
            patterns = s.patterns if s.kind == "amlworld" else None
            if self._in_thread:
                rep = await asyncio.to_thread(loader.load, s.trans, patterns, scope)
            else:
                rep = loader.load(s.trans, patterns, scope)
            if isinstance(rep, LoaderError):
                self.status, self.error = FeedStatus.FAILED, str(rep)
                log.error("dataset load failed: %s", rep)
                return
            self.report = rep
            clock = self.state.clock
            if rep.first_ts is not None and rep.first_ts > clock.now():
                clock.set(rep.first_ts)
            if s.warmup:
                n = loader.remaining if s.warmup_until is None else loader.count_before(s.warmup_until)
                self.status = FeedStatus.WARMING_UP
                self._set_warmup(True)
                t0 = self._monotonic()
                await loader.replay("max", count=n)
                await self._drain()
                helix = getattr(self.state, "helix", None)
                dur = max(0.0, self._monotonic() - t0)
                if helix is not None:
                    self.warmup_report = build_report(helix, n, dur, s.warmup_until)
                else:
                    self.warmup_report = WarmupReport(n, dur, n / dur if dur > 0 else 0.0, 0, 0, s.warmup_until)
                log.info("warm-up done: %s", self.warmup_report)
                self._set_warmup(False)
            self.status = FeedStatus.STREAMING
            if loader.remaining > 0 or not s.warmup:
                await loader.replay(s.speed)
            self.status = FeedStatus.DONE
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the server keeps running (design §24)
            self.status, self.error = FeedStatus.FAILED, f"{type(exc).__name__}: {exc}"
            log.exception("dataset feeder failed")
        finally:
            if s.warmup:
                self._set_warmup(False)

    # ------------------------------------------------------------------ status

    def progress(self) -> FeedProgress:
        p = self._loader.progress if self._loader is not None else LoaderProgress()
        try:
            reasons = dict(p.skip_reasons)
        except RuntimeError:  # pragma: no cover - resized by the loader thread mid-copy
            reasons = {}
        return FeedProgress(status=self.status, rows_read=p.rows_read, skipped=p.skipped, skip_reasons=reasons,
                            in_scope=p.in_scope, submitted=p.emitted, warmup=self.warmup_report, error=self.error)


__all__ = ["DEFAULT_MAX_ROWS", "DatasetFeeder", "DatasetSpec", "DatasetStartupError", "FeedProgress", "FeedStatus"]
