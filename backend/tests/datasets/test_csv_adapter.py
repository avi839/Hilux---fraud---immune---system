"""CSV_Adapter and Onboard_Validator (design §25): Properties 82, 83, 84 plus unit tests."""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from helix.codec import TransactionEvent
from helix.config.schema import DatasetConfig
from helix.datasets import HEADER, DatasetLoader, RowError, parse_row
from helix.datasets.amlworld import resolve_tz
from helix.datasets.csv_adapter import MappedRowParser, MappingCodec
from helix.datasets.mapping import ColumnMapping, load_mapping, validate_mapping
from helix.datasets.onboard import main as onboard_main
from helix.datasets.onboard import validate_file
from helix.sim import InMemoryGroundTruth, ManualClock
from tests.property.test_datasets_properties import valid_rows

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[3]
FIX = Path(__file__).resolve().parents[1] / "fixtures"
GENERIC_CSV = FIX / "onboarding" / "generic_bank.csv"
AML_CSV = FIX / "aml" / "HI-Tiny_Trans.csv"
GENERIC = load_mapping(REPO / "mappings" / "generic_bank.yaml")
AMLWORLD = load_mapping(REPO / "mappings" / "amlworld.yaml")
GENERIC_HEADER = ["TxnRef", "PostedAt", "DebitAccount", "CreditAccount", "Amount", "Currency", "Channel", "MCC",
                  "DeviceId", "Lat", "Lon", "IsFraud"]


class ListIngest:
    backpressure = False

    def __init__(self) -> None:
        self.events: list[TransactionEvent] = []

    def submit(self, ev: TransactionEvent) -> None:
        self.events.append(ev)


async def _no_sleep(_d: float) -> None:
    return None


# ------------------------------------------------------------------ unit tests


def test_generic_fixture_parses_with_expected_skips_and_london_time():
    assert isinstance(GENERIC, ColumnMapping) and isinstance(AMLWORLD, ColumnMapping)
    rep = validate_file(GENERIC, GENERIC_CSV)
    assert rep.rows_read == 30 and rep.parsed == 21
    assert dict(rep.rejected) == {
        "negative_amount": 1, "bad_amount": 1, "bad_timestamp": 1, "unknown_currency": 1, "bad_coordinate": 1,
        "wrong_column_count:8": 1, "amount_below_min": 1, "bad_account": 1, "amount_above_max": 1,
    }
    assert rep.examples["negative_amount"] == [5] and rep.labels["none"] == 2
    p = MappedRowParser(GENERIC, GENERIC_HEADER, "generic_bank")
    ev, lab = p.parse(["T9", "15/07/2024 12:00:00", "A", "B", "1,250.50", "gbp", "card", "", "", "", "", "Y"], 1)
    assert ev.ts == datetime(2024, 7, 15, 11, 0, tzinfo=UTC)  # BST = UTC+1
    assert ev.amount == Decimal("1250.50") and ev.currency == "GBP" and lab is True
    assert ev.mcc is None and ev.lat is None and "IsFraud" not in ev.model_dump()


def test_loader_with_mapping_writes_labels_only_to_ground_truth():
    ingest, gt = ListIngest(), InMemoryGroundTruth()
    loader = DatasetLoader(DatasetConfig(), ingest, ManualClock(datetime(2000, 1, 1, tzinfo=UTC)), gt,
                           sleep=_no_sleep, codec=MappingCodec(GENERIC))
    rep = loader.load(GENERIC_CSV)
    assert rep.in_scope == 21 and rep.skipped == 9 and rep.sort_mode == "memory"
    assert len(gt.records) == 19  # two rows have no label value
    assert {r.event_id for r in gt.records if r.label == "FRAUD"} == {
        "T0008", "T0009", "T0010", "T0016", "T0017", "T0018", "T0019"}
    asyncio.run(loader.replay("max"))
    ts = [e.ts for e in ingest.events]
    assert len(ingest.events) == 21 and ts == sorted(ts) and ingest.events[0].tx_id == "T0001"
    assert ingest.events[1].tx_id == "T0015"  # out-of-order row sorted into place
    for ev in ingest.events:
        assert set(ev.model_dump()) == set(TransactionEvent.model_fields)


def test_mapping_codec_rejects_a_header_without_the_mapped_columns():
    bound = MappingCodec(GENERIC).bind(["TxnRef", "PostedAt"], "x", UTC)
    assert isinstance(bound, str) and "DebitAccount" in bound


def test_onboard_cli_prints_report_and_fails_on_bad_mapping(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    rc = onboard_main(["--mapping", str(REPO / "mappings" / "generic_bank.yaml"), "--trans", str(GENERIC_CSV),
                       "--rows", "10"], out, err)
    text = out.getvalue()
    assert rc == 0 and "Mapping generic-bank v1 OK" in text and "Rows read 10: parsed 7, rejected 3" in text
    assert "negative_amount" in text and "Field coverage" in text and "Timestamps 2024-03-01T08:00:00Z" in text
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\ntimestamp: {source: {}, tz: Nowhere/City}\n", encoding="utf-8")
    rc = onboard_main(["--mapping", str(bad), "--trans", str(GENERIC_CSV)], out, err)
    assert rc == 1 and "timestamp.tz" in err.getvalue() and "src_account" in err.getvalue()


# ------------------------------------------------------------------ Property 82

RT_HEADER = ["Id", "When", "From1", "From2", "To", "Amt", "Ccy", "Chan", "Mcc", "Dev", "Sess", "Lat", "Lon", "Lab"]
ids = st.from_regex(r"\A[A-Za-z0-9]{1,20}\Z")
TS_FORMATS = {
    "iso8601": lambda dt: dt,
    "epoch_s": lambda dt: dt,
    "epoch_ms": lambda dt: dt.replace(microsecond=dt.microsecond // 1000 * 1000),
    "%d/%m/%Y %H:%M:%S": lambda dt: dt.replace(microsecond=0),
    "%Y-%m-%dT%H:%M:%S.%f": lambda dt: dt,
}


@st.composite
def mapping_and_event(draw):
    fmt = draw(st.sampled_from(sorted(TS_FORMATS)))
    tz = draw(st.sampled_from(["UTC", "+05:30", "Europe/London"]))
    dec, th = draw(st.sampled_from([(".", ""), (".", ","), (",", "."), (",", " ")]))
    two_src = draw(st.booleans())
    names = draw(st.sampled_from([{}, {"Pound Sterling": "GBP", "Dollar": "USD"}]))
    const_ccy = draw(st.booleans())
    doc: dict = {
        "name": "rt", "tx_id": {"name": "Id"},
        "timestamp": {"source": {"name": "When"}, "format": fmt, "tz": tz},
        "src_account": [{"name": "From1"}, {"name": "From2"}] if two_src else [{"name": "From1"}],
        "dst_account": [{"index": 4}],
        "amount": {"source": {"name": "Amt"}, "decimal": dec, "thousands": th},
        "currency": {"constant": "EUR"} if const_ccy else {"name": "Ccy"}, "currency_names": names,
    }
    optional = {"channel": "Chan", "merchant_category": "Mcc", "device_id": "Dev", "session_id": "Sess",
                "lat": "Lat", "lon": "Lon"}
    present = {f for f in optional if draw(st.booleans())}
    for f in present:
        doc[f] = {"name": optional[f]}
    with_label = draw(st.booleans())
    if with_label:
        doc["label"] = {"source": {"name": "Lab"}, "fraud_values": ["F", "fraud"], "legit_values": ["L"]}
    m = validate_mapping(doc)
    assert isinstance(m, ColumnMapping), m

    raw = draw(st.datetimes(min_value=datetime(1971, 1, 2), max_value=datetime(2099, 12, 30)))
    ts = TS_FORMATS[fmt](raw).replace(tzinfo=UTC)
    if fmt.startswith("%"):  # naive local wall time: must be unambiguous in the zone
        z = resolve_tz(tz)
        local = ts.astimezone(z).replace(tzinfo=None)
        assume(local.replace(tzinfo=z).astimezone(UTC) == ts)
    src = f"{draw(ids)}:{draw(ids)}" if two_src else draw(ids)
    ev_kw: dict = dict(
        tx_id=draw(ids), ts=ts, src=src, dst=draw(ids),
        amount=draw(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("999999999.99"), places=2)),
        currency="EUR" if const_ccy else draw(st.sampled_from(["GBP", "USD", "EUR", "XBT"])),
    )
    ev_field = {"channel": "channel", "merchant_category": "mcc", "device_id": "device_id",
                "session_id": "session_id"}
    for f, ef in ev_field.items():
        if f in present:
            ev_kw[ef] = draw(st.one_of(st.none(), ids))
    if "lat" in present:
        ev_kw["lat"] = draw(st.one_of(st.none(), st.floats(-90, 90, allow_nan=False)))
    if "lon" in present:
        ev_kw["lon"] = draw(st.one_of(st.none(), st.floats(-180, 180, allow_nan=False)))
    label = draw(st.sampled_from([True, False, None])) if with_label else None
    return m, TransactionEvent(**ev_kw), label


# Feature: helix-fraud-immune-system, Property 82: Mapped row round-trip
@pytest.mark.property
@given(case=mapping_and_event(), row=st.integers(1, 10**6))
def test_mapped_row_round_trip(case, row):
    """**Validates: Requirements 40.2, 40.5**"""
    m, ev, label = case
    p = MappedRowParser(m, RT_HEADER, "rt")
    fields = p.format(ev, label)
    assert len(fields) == len(RT_HEADER)
    res = p.parse(fields, row)
    assert not isinstance(res, RowError), (res, fields)
    got, got_label = res
    assert got == ev and got.amount == ev.amount and got_label == label


# ------------------------------------------------------------------ Property 83

CORRUPTIONS = {
    1: ("garbage", "bad_timestamp"),
    4: ("12x.5", "bad_amount"),
    5: ("US", "unknown_currency"),
    2: ("", "bad_account"),
}


@st.composite
def generic_events(draw, n: int):
    out = []
    t = datetime(2024, 1, 10, 9, 0, tzinfo=UTC)
    for i in range(n):
        t = t + timedelta(seconds=draw(st.integers(0, 4000)))
        out.append(TransactionEvent(
            tx_id=f"T{i}", ts=t, src=draw(ids), dst=draw(ids),
            amount=draw(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("99999.99"), places=2)),
            currency=draw(st.sampled_from(["GBP", "EUR"])),
            channel=draw(st.one_of(st.none(), ids)),
        ))
    return out


# Feature: helix-fraud-immune-system, Property 83: Unparseable rows are skipped without affecting valid rows
@pytest.mark.property
@given(data=st.data(), n=st.integers(1, 25))
def test_corrupted_rows_are_skipped_without_affecting_valid_rows(data, n):
    """**Validates: Requirements 40.3**"""
    events = data.draw(generic_events(n))
    labels = data.draw(st.lists(st.sampled_from([True, False, None]), min_size=n, max_size=n))
    p = MappedRowParser(GENERIC, GENERIC_HEADER, "bank")
    rows = [p.format(e, lab) for e, lab in zip(events, labels)]
    bad = data.draw(st.sets(st.integers(0, n - 1)))
    expected_reason = {}
    for i in sorted(bad):
        how = data.draw(st.sampled_from(sorted(CORRUPTIONS) + ["short"]))
        if how == "short":
            rows[i] = rows[i][:-2]
            expected_reason[i] = f"wrong_column_count:{len(GENERIC_HEADER) - 2}"
        else:
            col, (value, reason) = how, CORRUPTIONS[how]
            rows[i][col] = value
            expected_reason[i] = reason

    def run(selected):
        parsed, skipped = [], {}
        for i in selected:
            res = p.parse(rows[i], i + 1)
            if isinstance(res, RowError):
                skipped[i] = res.reason
            else:
                parsed.append(res)
        return parsed, skipped

    parsed, skipped = run(range(n))
    assert skipped == expected_reason
    assert len(parsed) + len(skipped) == n
    clean, none_skipped = run([i for i in range(n) if i not in bad])
    assert not none_skipped and parsed == clean
    assert [e for e, _ in clean] == [e for i, e in enumerate(events) if i not in bad]


# ------------------------------------------------------------------ Property 84


@st.composite
def aml_rows(draw) -> list[str]:
    f = list(draw(valid_rows()))
    mut = draw(st.sampled_from(["none", "none", "ts", "bank", "acct", "amount", "neg", "ccy", "label", "short",
                                "long", "channel", "huge_acct"]))
    if mut == "ts":
        f[0] = draw(st.sampled_from(["", "not-a-date", "2022-09-01", "2022/13/01 00:00", " 2022/09/01 00:10 "]))
    elif mut == "bank":
        f[1] = draw(st.sampled_from(["", "0x1", "\uff11\uff12", "-3", " 007 "]))
    elif mut == "acct":
        f[4] = draw(st.sampled_from(["", "   ", " 8000 "]))
    elif mut == "amount":
        f[7] = draw(st.sampled_from(["", "abc", "NaN", "Infinity", "1e999999", "0.004", "0.005", "1,000.00"]))
    elif mut == "neg":
        f[7] = "-" + f[7]
    elif mut == "ccy":
        f[8] = draw(st.sampled_from(["USD", "Pound", "", " Euro ", "euro"]))
    elif mut == "label":
        f[10] = draw(st.sampled_from(["2", "", "yes", " 1 "]))
    elif mut == "short":
        f = f[: draw(st.integers(0, 10))]
    elif mut == "long":
        f = f + ["extra"]
    elif mut == "channel":
        f[9] = "C" * 70
    elif mut == "huge_acct":
        f[2] = "A" * 70
    return f


# Feature: helix-fraud-immune-system, Property 84: AMLworld Column_Mapping agrees with the Dataset_Loader
@pytest.mark.property
@given(fields=aml_rows(), row=st.integers(1, 10**7))
def test_amlworld_mapping_agrees_with_dataset_loader(fields, row):
    """**Validates: Requirements 40.2, 40.10**"""
    stem = "HI-Small_Trans"
    want = parse_row(fields, row, stem, UTC)
    got = MappedRowParser(AMLWORLD, list(HEADER), stem).parse(fields, row)
    if isinstance(want, RowError):
        assert got == want
        return
    assert not isinstance(got, RowError), (got, want)
    ev, laund = want
    gev, glabel = got
    # Documented prefix difference: "amlworld-<stem>-<row>" vs "aml-<stem>-<row>".
    assert gev.tx_id == f"amlworld-{stem}-{row}" and ev.tx_id == f"aml-{stem}-{row}"
    assert gev.model_copy(update={"tx_id": ev.tx_id}) == ev and gev.amount == ev.amount
    assert glabel is laund


def test_amlworld_mapping_agrees_on_every_fixture_row():
    import csv

    with open(AML_CSV, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))[1:]
    p = MappedRowParser(AMLWORLD, list(HEADER), AML_CSV.stem)
    for i, f in enumerate(rows, start=1):
        want, got = parse_row(f, i, AML_CSV.stem, UTC), p.parse(f, i)
        if isinstance(want, RowError):
            assert got == want
        else:
            assert got[0].model_copy(update={"tx_id": want[0].tx_id}) == want[0] and got[1] is want[1]
