"""Property tests for helix.codec (Event_Codec)."""

from __future__ import annotations

import json
import re
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.codec import OPTIONAL_FIELDS, REQUIRED_FIELDS, CodecError, TransactionEvent, format, parse

SCHEMA_FIELDS = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)
ID_FIELDS = ("tx_id", "src", "dst", "channel", "mcc", "device_id", "session_id")

# Identifiers: any non-surrogate text of 1-64 code points.
ids = st.text(st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=64)
offsets = st.integers(min_value=-18 * 60, max_value=18 * 60).map(lambda m: timezone(timedelta(minutes=m)))
timestamps = st.datetimes(
    min_value=datetime(1970, 1, 1), max_value=datetime(2100, 1, 1), timezones=offsets
)
amounts = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("999999999.99"), places=2)
currencies = st.text(string.ascii_uppercase, min_size=3, max_size=3)
lats = st.floats(min_value=-90, max_value=90)
lons = st.floats(min_value=-180, max_value=180)


@st.composite
def events(draw) -> TransactionEvent:
    opt = {f: draw(st.none() | ids) for f in ("channel", "mcc", "device_id", "session_id")}
    opt["lat"] = draw(st.none() | lats)
    opt["lon"] = draw(st.none() | lons)
    return TransactionEvent(
        tx_id=draw(ids), ts=draw(timestamps), src=draw(ids), dst=draw(ids),
        amount=draw(amounts), currency=draw(currencies), **opt,
    )


json_scalars = st.none() | st.booleans() | st.integers() | st.text(max_size=10) | st.floats(allow_nan=False, allow_infinity=False)
extra_keys = st.dictionaries(st.text(max_size=12).filter(lambda k: k not in SCHEMA_FIELDS), json_scalars, max_size=4)


# Feature: helix-fraud-immune-system, Property 1: Codec round-trip
@pytest.mark.property
@given(ev=events(), extras=extra_keys)
def test_codec_round_trip(ev, extras):
    """**Validates: Requirements 2.1, 2.4, 2.5, 2.6, 2.7, 2.9**"""
    raw = format(ev)
    back = parse(raw)
    assert isinstance(back, TransactionEvent), back
    assert back == ev
    # Amount is the exact same Decimal, written with exactly 2 fractional digits.
    assert back.amount == ev.amount and back.amount.as_tuple() == ev.amount.as_tuple()
    doc = json.loads(raw)
    assert re.fullmatch(r"\d+\.\d{2}", doc["amount"])
    assert re.search(r"([+-]\d{2}:\d{2})$", doc["ts"])  # explicit UTC offset
    # Missing Optional_Fields are omitted; present ones are written.
    present = {f for f in OPTIONAL_FIELDS if getattr(ev, f) is not None}
    assert set(doc) == set(REQUIRED_FIELDS) | present
    # Null and absent parse identically.
    nulled = {**doc, **{f: None for f in OPTIONAL_FIELDS if f not in present}}
    assert parse(json.dumps(nulled)) == ev
    # Undefined keys do not change the parse result.
    assert parse(json.dumps({**extras, **doc})) == ev


# Per-field values that violate exactly that field's schema rule (None = delete the key).
VIOLATIONS: dict[str, st.SearchStrategy] = {
    **{f: st.sampled_from(["", "x" * 65, 123, True, ["a"]]) for f in ID_FIELDS},
    "ts": st.sampled_from([1717406100, "2024-06-03T09:15:00", "not-a-date", "", 3.5]),
    "amount": st.sampled_from(["0", "0.00", "-20.00", 0, -5, "1.234", "1000000000.00", "abc", True, None])
    | st.decimals(max_value=Decimal("0"), allow_nan=False, allow_infinity=False, places=2).map(str),
    "currency": st.sampled_from(["euro", "EU", "eur", "EURO", "E1R", "", 978]),
    "lat": st.sampled_from([90.5, -91, "north", True, 1e9]),
    "lon": st.sampled_from([180.01, -181, "east", False, -1e9]),
}


@st.composite
def invalid_documents(draw):
    doc = json.loads(format(draw(events())))
    broken = draw(st.sets(st.sampled_from(sorted(VIOLATIONS)), min_size=1))
    for f in sorted(broken):
        if f in REQUIRED_FIELDS and draw(st.booleans()):
            doc.pop(f)  # missing required field
        else:
            doc[f] = draw(VIOLATIONS[f])
    return json.dumps(doc), broken


# Feature: helix-fraud-immune-system, Property 2: Codec rejects invalid documents completely
@pytest.mark.property
@given(case=invalid_documents())
def test_codec_rejects_invalid_documents_completely(case):
    """**Validates: Requirements 1.10, 2.2, 2.3, 2.8**

    Codec side only; the malformed counter and rejected-events log belong to Stream_Ingestor.
    """
    raw, broken = case
    result = parse(raw)
    assert isinstance(result, CodecError)
    assert result.fields == broken
    assert all(e.rule for e in result.errors)
