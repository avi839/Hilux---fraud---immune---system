"""Property tests for helix.datasets (AMLworld Dataset_Loader): Properties 74-76."""

from __future__ import annotations

import asyncio
import csv
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.codec import TransactionEvent
from helix.datasets import HEADER, NAME_TO_CODE, DatasetLoader, LoadReport, ReplayScope, RowError, format_row, parse_row
from helix.config.schema import DatasetConfig
from helix.ingest import StreamIngestor
from helix.sim import InMemoryGroundTruth, ManualClock

UTC = timezone.utc
T0 = datetime(2022, 9, 1, tzinfo=UTC)
TZS = [UTC, timezone(timedelta(hours=2)), timezone(timedelta(hours=-5, minutes=-30)), timezone(timedelta(hours=14))]
FORMATS = ["Reinvestment", "Cheque", "Credit Card", "ACH", "Cash", "Wire", "Bitcoin", ""]
CURRENCY_NAMES = sorted(NAME_TO_CODE)


class RecordingIngestor(StreamIngestor):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.order: list[str] = []

    def submit(self, raw):
        self.order.append(raw.tx_id)
        return super().submit(raw)


async def _no_sleep(_d: float) -> None:
    return None


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)


def _aml_ts(dt: datetime) -> str:
    return f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"


# ------------------------------------------------------------------ strategies

naive_dts = st.datetimes(min_value=datetime(1971, 1, 1), max_value=datetime(2099, 12, 31))


@st.composite
def timestamps(draw) -> str:
    dt = draw(naive_dts)
    form = draw(st.sampled_from(["aml", "iso_naive", "iso_offset"]))
    if form == "aml":
        return _aml_ts(dt)
    if form == "iso_naive":
        return dt.isoformat(sep=draw(st.sampled_from(["T", " "])))
    off = timedelta(minutes=draw(st.integers(-12 * 60, 14 * 60)))
    return dt.replace(tzinfo=timezone(off)).isoformat()


banks = st.from_regex(r"\A[0-9]{1,6}\Z")
accounts = st.from_regex(r"\A[0-9A-F]{1,20}\Z")


@st.composite
def amounts(draw) -> str:
    places = draw(st.integers(0, 6))
    d = draw(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("99999999"), places=places))
    return str(d)


@st.composite
def valid_rows(draw) -> list[str]:
    return [
        draw(timestamps()), draw(banks), draw(accounts), draw(banks), draw(accounts),
        draw(amounts()), draw(st.sampled_from(CURRENCY_NAMES)),  # received side: ignored
        draw(amounts()), draw(st.sampled_from(CURRENCY_NAMES)),
        draw(st.sampled_from(FORMATS)), draw(st.sampled_from(["0", "1"])),
    ]


# ------------------------------------------------------------------ Property 74


# Feature: helix-fraud-immune-system, Property 74: Dataset_Loader CSV round-trip
@pytest.mark.property
@given(fields=valid_rows(), tz=st.sampled_from(TZS), row=st.integers(1, 10**7))
def test_csv_round_trip_and_label_never_on_event(fields, tz, row):
    """**Validates: Requirements 4.6, 4.10**"""
    first = parse_row(fields, row, "HI-Small_Trans", tz)
    assert not isinstance(first, RowError), first
    ev, laund = first
    out = format_row(ev, laund, tz)
    assert len(out) == len(HEADER)
    assert parse_row(out, row, "HI-Small_Trans", tz) == first
    # The label is never on the event: flipping it yields the identical event.
    flipped = list(fields)
    flipped[10] = "0" if fields[10] == "1" else "1"
    ev2, laund2 = parse_row(flipped, row, "HI-Small_Trans", tz)
    assert ev2 == ev and laund2 != laund
    assert set(ev.model_dump()) == set(TransactionEvent.model_fields)


# ------------------------------------------------------------------ Property 75


def _content_row(c: int, laundering: bool, pad: int = 0) -> list[str]:
    """Distinct contents c share some fields so near-misses are exercised."""
    ts = T0 + timedelta(minutes=c // 2)
    amt = "100.00" if c % 3 else "250.50"
    return [_aml_ts(ts), "0" * pad + str(10 + c % 2), f"A{c}", "20", "B", amt, "Euro", amt, "Euro", "Wire",
            "1" if laundering else "0"]


@st.composite
def pattern_cases(draw):
    n_contents = draw(st.integers(1, 4))
    loaded = draw(st.lists(st.tuples(st.integers(0, n_contents - 1), st.booleans()), min_size=1, max_size=15))
    entry = st.one_of(
        st.tuples(st.just("loaded"), st.integers(0, n_contents - 1), st.integers(0, 3)),
        st.tuples(st.just("none"), st.integers(100, 103), st.just(0)),  # content never loaded
    )
    blocks = draw(
        st.lists(
            st.tuples(st.sampled_from(["FAN-OUT", "CYCLE", "STACK", "RANDOM"]), st.lists(entry, min_size=1, max_size=6)),
            min_size=1, max_size=3,
        )
    )
    return loaded, blocks


# Feature: helix-fraud-immune-system, Property 75: Pattern rows map to exactly one loaded transaction or are counted unmatched
@pytest.mark.property
@settings(max_examples=60)
@given(case=pattern_cases())
def test_pattern_rows_match_one_tx_or_count_unmatched(case):
    """**Validates: Requirements 4.18, 4.19**"""
    loaded, blocks = case
    with tempfile.TemporaryDirectory() as d:
        trans = Path(d) / "HI-P_Trans.csv"
        _write_csv(trans, [_content_row(c, laund) for c, laund in loaded])
        lines: list[str] = []
        for ptype, entries in blocks:
            lines.append(f"BEGIN LAUNDERING ATTEMPT - {ptype}:  detail")
            for _kind, c, pad in entries:
                lines.append(",".join(_content_row(c, True, pad)))
            lines.append(f"END LAUNDERING ATTEMPT - {ptype}")
        pats = Path(d) / "HI-P_Patterns.txt"
        pats.write_text("\n".join(lines) + "\n", encoding="utf-8")
        gt = InMemoryGroundTruth()
        clock = ManualClock(T0)
        loader = DatasetLoader(DatasetConfig(), RecordingIngestor(clock=clock), clock, gt, sleep=_no_sleep)
        rep = loader.load(trans, pats)
    assert isinstance(rep, LoadReport)

    content_of = {f"aml-HI-P_Trans-{i}": c for i, (c, _) in enumerate(loaded, start=1)}
    fraud = {f"aml-HI-P_Trans-{i}" for i, (_, laund) in enumerate(loaded, start=1) if laund}
    supply = Counter(c for c, laund in loaded if laund)
    demand = Counter(c for _, entries in blocks for kind, c, _ in entries if kind == "loaded")
    nones = sum(1 for _, entries in blocks for kind, _, _ in entries if kind == "none")
    expected_unmatched = nones + sum(max(0, n - supply[c]) for c, n in demand.items())
    total = sum(len(e) for _, e in blocks)

    recs = [r for r in gt.records if r.pattern_id is not None]
    assert rep.unmatched_pattern_rows == expected_unmatched
    assert rep.pattern_rows_matched == len(recs) == total - expected_unmatched
    claimed = [r.event_id for r in recs]
    assert len(claimed) == len(set(claimed))  # never claimed twice, so never twice within a pattern
    for r in recs:
        assert r.event_id in fraud
        n = int(r.pattern_id.rsplit("-p", 1)[1])
        ptype, entries = blocks[n - 1]
        assert r.pattern_type == ptype
        assert content_of[r.event_id] in {c for kind, c, _ in entries if kind == "loaded"}


# ------------------------------------------------------------------ Property 76


@st.composite
def scope_cases(draw):
    rows = draw(st.lists(st.tuples(st.integers(0, 20), st.booleans()), max_size=25))
    start = draw(st.none() | st.integers(0, 21))
    end = draw(st.none() | st.integers(0, 21))
    max_rows = draw(st.none() | st.integers(0, 30))
    return rows, start, end, max_rows, draw(st.booleans())


# Feature: helix-fraud-immune-system, Property 76: Scope filtering emits exactly the in-window, under-limit rows in order
@pytest.mark.property
@settings(max_examples=80)
@given(case=scope_cases())
def test_scope_filtering_emits_exact_rows_in_order(case):
    """**Validates: Requirements 4.3, 4.16**"""
    rows, start, end, max_rows, use_duckdb = case
    at = lambda m: T0 + timedelta(minutes=m)  # noqa: E731
    csv_rows = []
    for i, (m, valid) in enumerate(rows, start=1):
        cur = "Euro" if valid else "Doubloon"
        csv_rows.append([_aml_ts(at(m)), "1", f"S{i % 3}", "2", "D", "1.00", cur, "1.00", cur, "Cash", str(i % 2)])
    scope = ReplayScope(
        start=None if start is None else at(start), end=None if end is None else at(end), max_rows=max_rows
    )
    with tempfile.TemporaryDirectory() as d:
        trans = Path(d) / "S_Trans.csv"
        _write_csv(trans, csv_rows)
        gt = InMemoryGroundTruth()
        clock = ManualClock(T0)
        ingest = RecordingIngestor(clock=clock)
        loader = DatasetLoader(
            DatasetConfig(), ingest, clock, gt, sleep=_no_sleep,
            sort_in_memory_max_bytes=0 if use_duckdb else 1 << 30,
        )
        rep = loader.load(trans, scope=scope)
        assert isinstance(rep, LoadReport)
        asyncio.run(loader.replay("max"))

    valid = sorted((m, i) for i, (m, ok) in enumerate(rows, start=1) if ok)
    window = [i for m, i in valid if (start is None or m >= start) and (end is None or m < end)]
    expected = [f"aml-S_Trans-{i}" for i in (window if max_rows is None else window[:max_rows])]
    assert ingest.order == expected
    labels = [r for r in gt.records if r.pattern_id is None]
    assert [r.event_id for r in labels] == expected
    for r in labels:
        i = int(r.event_id.rsplit("-", 1)[1])
        assert r.label == ("FRAUD" if i % 2 else "LEGIT")
