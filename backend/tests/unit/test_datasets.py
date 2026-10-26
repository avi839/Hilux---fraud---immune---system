"""Unit tests for helix.datasets (AMLworld Dataset_Loader), against tests/fixtures/aml."""

from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from helix.codec import TransactionEvent
from helix.config import ConfigValidationError
from helix.config.schema import DatasetConfig, IngestConfig
from helix.datasets import (
    HEADER,
    DatasetLoader,
    LoaderError,
    LoadReport,
    ReplayScope,
    RowError,
    format_row,
    motif_for,
    parse_patterns,
    parse_row,
)
from helix.ingest import StreamIngestor
from helix.motifs.models import MotifType
from helix.sim import InMemoryGroundTruth, ManualClock
from helix.sim.ground_truth import GTSource

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "aml"
TRANS = FIX / "HI-Tiny_Trans.csv"
PATTERNS = FIX / "HI-Tiny_Patterns.txt"
UTC = timezone.utc
T0 = datetime(2022, 9, 1, tzinfo=UTC)

# (ts, row) order of the 37 parseable fixture rows; row 38 is the deliberate late row.
EXPECTED_ORDER = [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 16, 17, 18, 19, 20, 22, 23, 24, 25, 26, 27, 38,
                  28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 39, 40]


class RecordingIngestor(StreamIngestor):
    def __init__(self, *a, log: list | None = None, **kw):
        super().__init__(*a, **kw)
        self.order: list[str] = []
        self._log = log

    def submit(self, raw):
        self.order.append(raw.tx_id)
        if self._log is not None:
            self._log.append(("emit", raw.tx_id))
        return super().submit(raw)


class LoggingGT(InMemoryGroundTruth):
    def __init__(self, log: list):
        super().__init__()
        self._log = log

    def write(self, rec):
        self._log.append(("label", rec.event_id))
        super().write(rec)


class FakeTime:
    """Injected sleep + monotonic: sleeping advances time instantly."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep = None

    async def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        self.t += max(d, 0.0)
        if self.on_sleep is not None:
            self.on_sleep()

    def monotonic(self) -> float:
        return self.t


def make(cfg: DatasetConfig | None = None, *, ingest=None, gt=None, clock=None, fake=None, **kw):
    # `is None` checks: an empty StreamIngestor is falsy (it defines __len__).
    clock = clock if clock is not None else ManualClock(datetime(2022, 1, 1, tzinfo=UTC))
    ingest = ingest if ingest is not None else RecordingIngestor(clock=clock)
    gt = gt if gt is not None else InMemoryGroundTruth()
    fake = fake if fake is not None else FakeTime()
    loader = DatasetLoader(cfg or DatasetConfig(), ingest, clock, gt, sleep=fake.sleep, monotonic=fake.monotonic, **kw)
    return loader, ingest, gt, clock, fake


def rows_of(ids):
    return [int(t.rsplit("-", 1)[1]) for t in ids]


def label_records(gt):
    return [r for r in gt.records if r.pattern_id is None]


# ------------------------------------------------------------------ parse / format


def test_header_order_and_format_row_round_trip_on_fixture():
    with open(TRANS, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        assert tuple(next(reader)) == HEADER
        for row, fields in enumerate(reader, start=1):
            res = parse_row(fields, row, "HI-Tiny", UTC)
            if isinstance(res, RowError):
                continue
            ev, laund = res
            out = format_row(ev, laund, UTC)
            assert len(out) == len(HEADER)
            assert parse_row(out, row, "HI-Tiny", UTC) == (ev, laund)


def test_parse_row_mapping_and_ignored_received_columns():
    fields = ["2022/09/01 00:20", "0011", "800059120", "0021", "8000EBD30",
              "14.09", "UK Pound", "15.53", "Euro", "Credit Card", "1"]
    ev, laund = parse_row(fields, 12, "HI-Tiny", UTC)
    assert ev == TransactionEvent(
        tx_id="aml-HI-Tiny-12", ts=datetime(2022, 9, 1, 0, 20, tzinfo=UTC), src="11:800059120",
        dst="21:8000EBD30", amount=Decimal("15.53"), currency="EUR", channel="Credit Card",
    )
    assert laund is True
    assert ev.mcc is ev.lat is ev.lon is ev.device_id is ev.session_id is None
    assert "laundering" not in ev.model_dump()


@pytest.mark.parametrize(
    "idx,value,reason",
    [
        (0, "not-a-date", "bad_timestamp"),
        (0, "2022-09-01", "bad_timestamp"),
        (1, "x10", "bad_account"),
        (2, "", "bad_account"),
        (7, "abc", "bad_amount"),
        (7, "0.004", "amount_below_min"),
        (7, "-5", "amount_below_min"),
        (7, "1e12", "amount_above_max"),
        (8, "Doubloon", "unknown_currency"),
        (10, "2", "bad_label"),
    ],
)
def test_parse_row_errors(idx, value, reason):
    fields = ["2022/09/01 00:20", "1", "A", "2", "B", "1", "Euro", "1.00", "Euro", "Wire", "0"]
    fields[idx] = value
    assert parse_row(fields, 7, "s", UTC) == RowError(7, reason)
    assert parse_row(fields[:10], 7, "s", UTC) == RowError(7, "wrong_column_count:10")


def test_timezone_naive_and_explicit_offset():
    tz = timezone(timedelta(hours=2))
    base = ["", "1", "A", "2", "B", "1", "Euro", "1.00", "Bitcoin", "", "0"]
    naive = list(base); naive[0] = "2022/09/01 02:20"
    ev, _ = parse_row(naive, 1, "s", tz)
    assert ev.ts == datetime(2022, 9, 1, 0, 20, tzinfo=UTC)
    assert ev.currency == "XBT" and ev.channel is None
    explicit = list(base); explicit[0] = "2022-09-01T02:20:15Z"
    ev2, _ = parse_row(explicit, 2, "s", tz)
    assert ev2.ts == datetime(2022, 9, 1, 2, 20, 15, tzinfo=UTC)
    # Seconds are kept on the round trip via the ISO fallback.
    assert parse_row(format_row(ev2, False, tz), 2, "s", tz) == (ev2, False)


# ------------------------------------------------------------------ load


def test_load_fixture_counts_order_and_labels(caplog):
    loader, ingest, gt, _, _ = make()
    with caplog.at_level(logging.WARNING, logger="helix.datasets"):
        rep = loader.load(TRANS)
    assert isinstance(rep, LoadReport)
    assert (rep.parsed, rep.skipped, rep.out_of_scope, rep.laundering) == (37, 3, 0, 12)
    assert rep.skip_reasons == {"amount_below_min": 1, "unknown_currency": 1, "bad_timestamp": 1}
    assert rep.sort_mode == "memory" and not rep.sorted_externally
    assert rep.first_ts == T0 and rep.last_ts == T0 + timedelta(hours=1, minutes=25)
    text = caplog.text
    assert "row 10 skipped: amount_below_min" in text
    assert "row 15 skipped: unknown_currency" in text
    assert "row 21 skipped: bad_timestamp" in text

    evs = loader.events
    assert rows_of(e.tx_id for e in evs) == EXPECTED_ORDER
    by_row = {int(e.tx_id.rsplit("-", 1)[1]): e for e in evs}
    assert by_row[1].src == by_row[35].src == "10:8000EBD30"  # 010 and 0010 normalise
    assert by_row[18].ts == T0 + timedelta(minutes=33)  # ISO row
    assert (by_row[36].amount, by_row[36].currency) == (Decimal("0.55"), "XBT")
    assert (by_row[12].amount, by_row[12].currency) == (Decimal("15.53"), "EUR")

    labels = label_records(gt)
    assert [r.event_id for r in labels] == [e.tx_id for e in evs]
    assert all(r.source is GTSource.LOADER for r in labels)
    fraud = {int(r.event_id.rsplit("-", 1)[1]) for r in labels if r.label == "FRAUD"}
    assert fraud == {7, 9, 11, 14, 16, 19, 22, 24, 26, 28, 30, 32}
    assert ingest.order == []  # nothing emitted by load()


def test_duckdb_sort_matches_in_memory_sort():
    mem, _, gt_mem, _, _ = make()
    duck, _, gt_duck, _, _ = make(sort_in_memory_max_bytes=0)
    r1, r2 = mem.load(TRANS, PATTERNS), duck.load(TRANS, PATTERNS)
    assert r1.sort_mode == "memory" and r2.sort_mode == "duckdb" and r2.sorted_externally
    assert mem.events == duck.events  # same tx_ids => DuckDB row == csv.reader row
    strip = lambda gt: [(r.event_id, r.label, r.pattern_id, r.pattern_type) for r in gt.records]
    assert strip(gt_mem) == strip(gt_duck)


def test_ordered_file_streams_without_sort(tmp_path):
    lines = TRANS.read_text(encoding="utf-8").splitlines()
    del lines[38]  # header is line 0, so this drops data row 38
    p = tmp_path / "ordered_Trans.csv"
    p.write_text("\ufeff" + "\n".join(lines) + "\n", encoding="utf-8")  # with BOM
    loader, *_ = make()
    rep = loader.load(p, scope=ReplayScope(end=T0 + timedelta(minutes=30)))
    assert rep.sort_mode == "none"
    assert rep.stopped_early and rep.rows_read < 39
    assert rows_of(e.tx_id for e in loader.events) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 16]


@pytest.mark.parametrize("duck", [False, True])
def test_scope_window_and_max_rows(duck):
    kw = {"sort_in_memory_max_bytes": 0} if duck else {}
    scope = ReplayScope(start=T0 + timedelta(minutes=30), end=T0 + timedelta(hours=1))
    loader, _, gt, _, _ = make(**kw)
    rep = loader.load(TRANS, scope=scope)
    assert rows_of(e.tx_id for e in loader.events) == [17, 18, 19, 20, 22, 23, 24, 25, 26, 27, 38, 28]
    assert rep.in_scope == 12 and rep.out_of_scope >= 1
    loader2, _, gt2, _, _ = make(**kw)
    loader2.load(TRANS, scope=ReplayScope(start=scope.start, end=scope.end, max_rows=5))
    assert rows_of(e.tx_id for e in loader2.events) == [17, 18, 19, 20, 22]
    assert rows_of(r.event_id for r in label_records(gt2)) == [17, 18, 19, 20, 22]


def test_scope_from_config():
    cfg = DatasetConfig(scope_start=T0 + timedelta(hours=1, minutes=20), scope_max_rows=1)
    loader, *_ = make(cfg)
    loader.load(TRANS)
    assert rows_of(e.tx_id for e in loader.events) == [39]


def test_missing_unreadable_and_bad_header_produce_no_labels(tmp_path):
    bad_utf8 = tmp_path / "bad_Trans.csv"
    bad_utf8.write_bytes(TRANS.read_bytes() + b"2022/09/01 02:00,1,\xff\xfe,2,B,1,Euro,1.00,Euro,Wire,0\n")
    short = tmp_path / "short_Trans.csv"
    short.write_text("Timestamp,From Bank,Account,To Bank,Account,Amount Received\n", encoding="utf-8")
    empty = tmp_path / "empty_Trans.csv"
    empty.write_text("", encoding="utf-8")
    cases = {
        tmp_path / "nope_Trans.csv": "file not found",
        bad_utf8: "unreadable",
        short: "missing or misplaced column(s): 7:Receiving Currency",
        empty: "no header",
    }
    for p, reason in cases.items():
        loader, ingest, gt, _, _ = make()
        err = loader.load(p, PATTERNS)
        assert isinstance(err, LoaderError), p
        assert err.path == p and reason in err.reason and str(p) in str(err)
        assert gt.records == [] and loader.events == ()
        with pytest.raises(RuntimeError):
            asyncio.run(loader.replay("max"))
        assert ingest.order == []
    loader, _, gt, _, _ = make()
    err = loader.load(TRANS, tmp_path / "missing_Patterns.txt")
    assert isinstance(err, LoaderError) and gt.records == []


def test_unknown_timezone_is_an_error():
    loader, _, gt, _, _ = make(DatasetConfig(timezone="Not/AZone"))
    err = loader.load(TRANS)
    assert isinstance(err, LoaderError) and "dataset.timezone" in err.reason and gt.records == []


# ------------------------------------------------------------------ patterns


def test_patterns_fixture_matching(caplog):
    loader, _, gt, _, _ = make()
    with caplog.at_level(logging.WARNING, logger="helix.datasets"):
        rep = loader.load(TRANS, PATTERNS)
    assert rep.patterns == {"FAN-OUT": 1, "CYCLE": 1, "GATHER-SCATTER": 1}
    assert (rep.pattern_rows_matched, rep.unmatched_pattern_rows) == (12, 1)
    assert "pattern row (line 20) unmatched" in caplog.text
    members: dict[tuple[str, str], list[int]] = {}
    for r in gt.records:
        if r.pattern_id is not None:
            assert r.label == "FRAUD" and r.source is GTSource.LOADER
            members.setdefault((r.pattern_id, r.pattern_type), []).append(int(r.event_id.rsplit("-", 1)[1]))
    assert members == {
        ("HI-Tiny_Patterns-p1", "FAN-OUT"): [7, 9, 11, 14, 16],  # "70" in the file matches bank "070"
        ("HI-Tiny_Patterns-p2", "CYCLE"): [19, 22, 24],
        ("HI-Tiny_Patterns-p3", "GATHER-SCATTER"): [26, 28, 30, 32],
    }
    # Every label precedes the pattern records.
    first_pattern = next(i for i, r in enumerate(gt.records) if r.pattern_id is not None)
    assert all(r.pattern_id is None for r in gt.records[:first_pattern])
    assert len(label_records(gt)) == first_pattern


def test_patterns_out_of_scope_rows_are_unmatched():
    loader, *_ = make()
    rep = loader.load(TRANS, PATTERNS, ReplayScope(end=T0 + timedelta(minutes=20)))
    # FAN-OUT rows 7, 9, 11 are in scope; 2 FAN-OUT + 3 CYCLE + 5 GATHER-SCATTER rows are not.
    assert (rep.pattern_rows_matched, rep.unmatched_pattern_rows) == (3, 10)
    assert rep.patterns == {"FAN-OUT": 1}


def test_parse_patterns_tolerant_header_unknown_type_and_stray_rows():
    row = "2022/09/01 00:10,070,100428660,1124,800825340,1810.14,US Dollar,1810.14,US Dollar,ACH,1"
    parsed = parse_patterns(
        [row, "", "begin laundering attempt - fan-in", row, "END LAUNDERING ATTEMPT - FAN-IN",
         "BEGIN LAUNDERING ATTEMPT - STACK:  Max 4 hops", row,
         "BEGIN LAUNDERING ATTEMPT - WEIRD: x", row]  # missing END twice
    )
    assert [r.lineno for r in parsed.stray_rows] == [1]
    assert [(b.n, b.pattern_type, b.detail, b.known, len(b.rows)) for b in parsed.blocks] == [
        (1, "FAN-IN", None, True, 1),
        (2, "STACK", "Max 4 hops", True, 1),
        (3, "WEIRD", "x", False, 1),
    ]


def test_motif_mapping():
    assert motif_for("CYCLE") is MotifType.ROUND_TRIP
    assert motif_for("FAN-OUT") is MotifType.FAN_OUT
    assert motif_for("fan-in") is MotifType.FAN_IN
    # Composed fan patterns map to their first stage (task 29.1).
    assert motif_for("GATHER-SCATTER") is MotifType.FAN_IN
    assert motif_for("scatter-gather") is MotifType.FAN_OUT
    for t in ("STACK", "RANDOM", "BIPARTITE"):
        assert motif_for(t) is None


# ------------------------------------------------------------------ replay


def test_labels_precede_emission_and_max_mode():
    events_log: list = []
    clock = ManualClock(datetime(2022, 1, 1, tzinfo=UTC))
    ingest = RecordingIngestor(clock=clock, log=events_log)
    loader, _, _, _, fake = make(ingest=ingest, gt=LoggingGT(events_log), clock=clock)
    loader.load(TRANS, PATTERNS)
    rep = asyncio.run(loader.replay("max"))
    assert rep.emitted == 37 and rows_of(ingest.order) == EXPECTED_ORDER
    first_label = {}
    for i, (kind, tx) in enumerate(events_log):
        if kind == "label":
            first_label.setdefault(tx, i)
        else:
            assert first_label[tx] < i
    assert all(d == 0 for d in fake.sleeps)  # no pacing in max mode
    with pytest.raises(RuntimeError):  # one-shot
        asyncio.run(loader.replay("max"))


def test_max_mode_pauses_on_backpressure():
    clock = ManualClock(datetime(2022, 1, 1, tzinfo=UTC))
    ingest = RecordingIngestor(IngestConfig(backpressure_limit=4), clock=clock)
    fake = FakeTime()
    drained: list[str] = []
    fake.on_sleep = lambda: drained.extend(a.event.tx_id for a in ingest.drain()) if fake.sleeps[-1] > 0 else None
    loader, *_ = make(ingest=ingest, clock=clock, fake=fake, backpressure_poll_s=0.01)
    loader.load(TRANS)
    max_queue = 0
    orig = ingest.submit

    def submit(ev):
        nonlocal max_queue
        assert not ingest.backpressure  # never submits while backpressure is signalled
        r = orig(ev)
        max_queue = max(max_queue, len(ingest))
        return r

    ingest.submit = submit
    rep = asyncio.run(loader.replay("max"))
    drained.extend(a.event.tx_id for a in ingest.drain())
    assert rep.emitted == 37 and max_queue <= 5
    assert sorted(drained) == sorted(ingest.order)
    assert any(d == 0.01 for d in fake.sleeps)


def test_multiplier_pacing_with_injected_sleep():
    loader, ingest, _, clock, fake = make()
    loader.load(TRANS, scope=ReplayScope(max_rows=6))
    seen = []
    orig = ingest.submit
    ingest.submit = lambda ev: (seen.append((fake.t, clock.now(), ev.ts)), orig(ev))[1]
    t_start = fake.t
    rep = asyncio.run(loader.replay(60))
    assert rep.emitted == 6 and rep.late_emissions == 0 and rep.max_jitter_s < 1e-6
    for wall, sim_now, ts in seen:
        assert wall == pytest.approx(t_start + (ts - T0).total_seconds() / 60)
        assert sim_now == ts  # SimClock set to the event ts at emission


@pytest.mark.parametrize("speed", [0, 0.5, 1_000_001, float("nan"), "fast", True, -1])
def test_invalid_speed_refuses_to_start(speed):
    loader, ingest, *_ = make()
    loader.load(TRANS)
    with pytest.raises(ConfigValidationError) as ei:
        asyncio.run(loader.replay(speed))
    assert ei.value.errors[0].path == "dataset.speed"
    assert ingest.order == []


@pytest.mark.parametrize("speed", [1, 1_000_000, "max"])
def test_valid_speed_bounds(speed):
    loader, ingest, *_ = make()
    loader.load(TRANS, scope=ReplayScope(max_rows=2))
    assert asyncio.run(loader.replay(speed)).emitted == 2
