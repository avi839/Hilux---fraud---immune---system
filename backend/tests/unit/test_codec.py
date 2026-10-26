"""Unit tests for helix.codec (Event_Codec). Req 1.10 (codec-error side), 2.1-2.7, 2.9."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from helix.codec import ROOT, CodecError, TransactionEvent, format, parse

SAMPLE = Path(__file__).resolve().parents[3] / "docs" / "sample_transactions.jsonl"

BASE = {
    "tx_id": "T001",
    "ts": "2024-06-03T09:15:00Z",
    "src": "ACC-ALICE",
    "dst": "MER-GROCER",
    "amount": "42.50",
    "currency": "EUR",
}


def doc(**overrides) -> str:
    d = {**BASE, **overrides}
    return json.dumps({k: v for k, v in d.items() if v is not ...})


def ok(raw) -> TransactionEvent:
    ev = parse(raw)
    assert isinstance(ev, TransactionEvent), ev
    return ev


def err(raw) -> CodecError:
    e = parse(raw)
    assert isinstance(e, CodecError), e
    return e


# --- Req 2.1 / 2.7: parsing and exact amounts ---------------------------------


def test_parses_minimal_document():
    ev = ok(doc())
    assert ev.tx_id == "T001"
    assert ev.ts == datetime(2024, 6, 3, 9, 15, tzinfo=timezone.utc)
    assert ev.amount == Decimal("42.50") and str(ev.amount) == "42.50"
    assert ev.currency == "EUR"


@pytest.mark.parametrize(
    "raw_amount, expected",
    [('"5"', "5.00"), ("5", "5.00"), ("0.10", "0.10"), ('"0.10"', "0.10"), ("0.01", "0.01"),
     ("999999999.99", "999999999.99"), ("1e2", "100.00")],
)
def test_amount_is_exact_two_place_decimal(raw_amount, expected):
    raw = doc(amount="__A__").replace('"__A__"', raw_amount)
    ev = ok(raw)
    assert str(ev.amount) == expected
    assert ev.amount.as_tuple().exponent == -2


def test_amount_from_json_number_not_read_through_binary_float():
    # A JSON number whose text has more than 2 decimals is rejected, never rounded.
    raw = doc(amount="__A__").replace('"__A__"', "0.1000000000000000055511151231257827")
    assert "amount" in err(raw).fields


# --- Req 2.4: optional fields explicitly missing ------------------------------


def test_missing_and_null_optionals_are_none_and_distinct_from_falsy_values():
    absent = ok(doc())
    null = ok(doc(channel=None, mcc=None, lat=None, lon=None, device_id=None, session_id=None))
    assert absent == null
    for f in ("channel", "mcc", "lat", "lon", "device_id", "session_id"):
        assert getattr(absent, f) is None
    zero = ok(doc(lat=0.0, lon=0, channel="c"))
    assert zero.lat == 0.0 and zero.lat is not None and zero.lon == 0.0
    assert zero != absent
    # Empty string is not "missing": it violates the 1-64 identifier rule.
    assert err(doc(channel="")).fields == {"channel"}


# --- Req 2.5: formatting -------------------------------------------------------


def test_format_two_decimals_offset_and_omits_missing():
    ev = ok(doc(amount=5, lat=52.52))
    out = json.loads(format(ev))
    assert out["amount"] == "5.00"
    assert out["ts"] == "2024-06-03T09:15:00+00:00"
    assert out["lat"] == 52.52
    assert set(out) == {"tx_id", "ts", "src", "dst", "amount", "currency", "lat"}


def test_format_keeps_non_utc_offset():
    tz = timezone(timedelta(hours=5, minutes=30))
    ev = TransactionEvent(tx_id="T", ts=datetime(2024, 1, 1, 12, tzinfo=tz), src="A", dst="B",
                          amount=Decimal("1"), currency="INR")
    assert json.loads(format(ev))["ts"] == "2024-01-01T12:00:00+05:30"
    assert parse(format(ev)) == ev


# --- Req 2.9: undefined fields ignored ----------------------------------------


def test_undefined_fields_ignored():
    assert ok(doc(foo="bar", nested={"x": 1})) == ok(doc())


# --- Req 2.2 / 2.3 / 1.10: rejection -------------------------------------------


@pytest.mark.parametrize(
    "overrides, field, rule",
    [
        ({"amount": "0"}, "amount", "greater_than_equal"),
        ({"amount": "-20.00"}, "amount", "greater_than_equal"),
        ({"amount": 0}, "amount", "greater_than_equal"),
        ({"amount": "1.234"}, "amount", "decimal_max_places"),
        ({"amount": "1000000000.00"}, "amount", "less_than_equal"),
        ({"amount": True}, "amount", "value_error"),
        ({"currency": "euro"}, "currency", "string_pattern_mismatch"),
        ({"currency": "EU"}, "currency", "string_pattern_mismatch"),
        ({"ts": 1717406100}, "ts", "value_error"),
        ({"ts": "2024-06-03T09:15:00"}, "ts", "timezone_aware"),
        ({"ts": "yesterday"}, "ts", "datetime_from_date_parsing"),
        ({"tx_id": "x" * 65}, "tx_id", "string_too_long"),
        ({"src": ""}, "src", "string_too_short"),
        ({"dst": 7}, "dst", "string_type"),
        ({"lat": 90.5}, "lat", "less_than_equal"),
        ({"lon": -180.01}, "lon", "greater_than_equal"),
        ({"lat": True}, "lat", "value_error"),
        ({"tx_id": ...}, "tx_id", "missing"),
    ],
)
def test_single_violation_names_field_and_rule(overrides, field, rule):
    e = err(doc(**overrides))
    assert e.fields == {field}
    assert any(x.field == field and x.rule == rule for x in e.errors), e


def test_every_violation_listed_not_just_first():
    e = err(doc(amount="-1", currency="euro", lat=200, ts=..., device_id=""))
    assert e.fields == {"amount", "currency", "lat", "ts", "device_id"}


@pytest.mark.parametrize("raw", ['{"tx_id": "T1",', "not json", "", b"\xff\xfe", "[1, 2]", "null", '{"amount": NaN}'])
def test_malformed_documents_return_error(raw):
    e = err(raw)
    assert e.errors  # never empty
    if raw not in ('{"amount": NaN}',):
        assert e.fields == {ROOT}


def test_bytes_input_accepted():
    assert ok(doc().encode("utf-8")).tx_id == "T001"


# --- docs/sample_transactions.jsonl -------------------------------------------


def test_sample_transactions_file():
    lines = [ln for ln in SAMPLE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    results = [parse(ln) for ln in lines]
    parsed = [r for r in results if isinstance(r, TransactionEvent)]
    errors = {json.loads(ln)["tx_id"]: r for ln, r in zip(lines, results) if isinstance(r, CodecError)}
    assert len(parsed) == 14
    assert set(errors) == {"T009"}
    assert errors["T009"].fields == {"amount", "currency"}
    # T006 omits lat/lon/device_id: explicitly missing.
    t006 = next(e for e in parsed if e.tx_id == "T006")
    assert t006.lat is None and t006.lon is None and t006.device_id is None
    # Every parsed event round-trips.
    assert all(parse(format(e)) == e for e in parsed)
