"""Column_Mapping validation and document round-trip (design §25): Properties 80, 81."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.datasets.mapping import (
    ColumnMapping,
    MappingError,
    dump_mapping,
    header_errors,
    load_mapping,
    parse_mapping,
    validate_mapping,
)

REPO = Path(__file__).resolve().parents[3]
MAPPINGS = REPO / "mappings"
GENERIC_HEADER = ["TxnRef", "PostedAt", "DebitAccount", "CreditAccount", "Amount", "Currency", "Channel", "MCC",
                  "DeviceId", "Lat", "Lon", "IsFraud"]

BASE: dict = {
    "name": "bank",
    "tx_id": {"name": "TxnRef"},
    "timestamp": {"source": {"name": "PostedAt"}, "format": "%d/%m/%Y %H:%M:%S", "tz": "Europe/London"},
    "src_account": [{"name": "DebitAccount"}],
    "dst_account": [{"name": "CreditAccount"}],
    "amount": {"source": {"name": "Amount"}, "thousands": ","},
    "currency": {"name": "Currency"},
    "label": {"source": {"name": "IsFraud"}, "fraud_values": ["Y"], "legit_values": ["N"]},
}


# ------------------------------------------------------------------ unit tests


def test_shipped_mappings_are_valid_and_match_their_fixture_headers():
    for f in ("amlworld.yaml", "generic_bank.yaml"):
        m = load_mapping(MAPPINGS / f)
        assert isinstance(m, ColumnMapping), m
    m = load_mapping(MAPPINGS / "generic_bank.yaml")
    assert header_errors(m, GENERIC_HEADER) == []
    assert m.timestamp.tz == "Europe/London" and m.amount.thousands == "," and m.label is not None


def test_base_document_is_valid_and_json_file_loads(tmp_path):
    m = validate_mapping(BASE)
    assert isinstance(m, ColumnMapping) and m.version == 1 and m.join == ":"
    p = tmp_path / "m.json"
    p.write_text(json.dumps(BASE), encoding="utf-8")
    assert load_mapping(p) == m


def test_header_errors_name_the_missing_column_and_index():
    m = validate_mapping({**BASE, "channel": {"index": 40}})
    assert isinstance(m, ColumnMapping)
    errs = header_errors(m, [h for h in GENERIC_HEADER if h != "Amount"])
    assert {e.path for e in errs} == {"amount.source.name", "channel.index"}
    assert any("'Amount'" in e.reason for e in errs)
    assert header_errors(m, None) == [MappingError("$", "empty file: no header row")]


def test_load_errors_for_missing_and_unparseable_files(tmp_path):
    assert "file not found" in load_mapping(tmp_path / "nope.yaml")[0].reason
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [unclosed\n", encoding="utf-8")
    assert load_mapping(bad)[0].path == "$"
    assert validate_mapping(["not", "a", "mapping"])[0].path == "$"


def test_unknown_field_error_names_the_field():
    errs = validate_mapping({**BASE, "iban": {"name": "IBAN"}})
    assert errs == [MappingError("iban", "unknown field (not a Transaction_Event field or mapping option)")]


# ------------------------------------------------------------------ Property 80


def _set(path: tuple, value):
    def f(d):
        cur = d
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = value
    return f


def _pop(path: tuple):
    def f(d):
        cur = d
        for k in path[:-1]:
            cur = cur[k]
        cur.pop(path[-1])
    return f


VIOLATIONS = {
    "missing_dst": (_pop(("dst_account",)), "dst_account"),
    "empty_src": (_set(("src_account",), []), "src_account"),
    "missing_ts_source": (_pop(("timestamp", "source")), "timestamp.source"),
    "unknown_field": (_set(("iban",), {"name": "IBAN"}), "iban"),
    "bad_tz": (_set(("timestamp", "tz"), "Mars/Olympus_Mons"), "timestamp.tz"),
    "bad_format": (_set(("timestamp", "format"), "not-a-format"), "timestamp.format"),
    "bad_ccy_constant": (_set(("currency",), {"constant": "usd"}), "currency.constant"),
    "bad_ccy_table": (_set(("currency_names",), {"Pound": "gbp"}), "currency_names.Pound"),
    "two_sources": (_set(("amount", "source"), {"name": "Amount", "index": 4}), "amount.source"),
    "no_source": (_set(("channel",), {"transform": "upper"}), "channel"),
    "label_overlap": (_set(("label", "legit_values"), ["N", "Y"]), "label"),
}


# Feature: helix-fraud-immune-system, Property 80: Column_Mapping validation reports every error
@pytest.mark.property
@given(chosen=st.sets(st.sampled_from(sorted(VIOLATIONS)), min_size=1))
def test_validation_reports_exactly_the_violated_paths(chosen):
    """**Validates: Requirements 40.1**"""
    doc = copy.deepcopy(BASE)
    for k in sorted(chosen):
        VIOLATIONS[k][0](doc)
    res = validate_mapping(doc)
    assert isinstance(res, list), "an invalid mapping must not be returned"
    assert {e.path for e in res} == {VIOLATIONS[k][1] for k in chosen}
    assert all(e.reason for e in res)


# ------------------------------------------------------------------ Property 81

texts = st.text(st.characters(codec="utf-8", categories=["L", "N", "P", "Zs"]), min_size=1, max_size=8)
transforms = st.sampled_from(["none", "strip", "int", "upper"])


@st.composite
def refs(draw, constant_ok: bool = True):
    kind = draw(st.sampled_from(["name", "index", "constant"] if constant_ok else ["name", "index"]))
    v = {"name": draw(texts), "index": draw(st.integers(0, 40)), "constant": draw(texts)}[kind]
    out = {kind: v}
    if draw(st.booleans()):
        out["transform"] = draw(transforms)
    return out


@st.composite
def mapping_docs(draw) -> dict:
    fmt = st.sampled_from(["iso8601", "epoch_s", "epoch_ms", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%y %I:%M %p"])
    dec, th = draw(st.sampled_from([(".", ""), (".", ","), (",", "."), (",", " "), (".", "'")]))
    doc: dict = {
        "name": draw(st.from_regex(r"\A[a-z][a-z0-9-]{0,15}\Z")),
        "delimiter": draw(st.sampled_from([",", ";", "\t", "|"])),
        "encoding": draw(st.sampled_from(["utf-8", "utf-8-sig", "latin-1", "cp1252"])),
        "timestamp": {"source": draw(refs(False)), "format": draw(fmt),
                      "alt_formats": draw(st.lists(fmt, max_size=2)),
                      "tz": draw(st.sampled_from(["UTC", "Europe/London", "America/New_York", "+05:30", "UTC-3"]))},
        "src_account": draw(st.lists(refs(False), min_size=1, max_size=3)),
        "dst_account": draw(st.lists(refs(False), min_size=1, max_size=3)),
        "join": draw(st.sampled_from([":", "-", "/", ""])),
        "amount": {"source": draw(refs(False)), "decimal": dec, "thousands": th,
                   "negative": draw(st.sampled_from(["reject", "abs", "below_min"]))},
        "currency": draw(st.one_of(refs(False), st.sampled_from([{"constant": "USD"}, {"constant": "XBT"}]))),
        "currency_names": draw(st.dictionaries(texts, st.from_regex(r"\A[A-Z]{3}\Z"), max_size=3)),
        "currency_strict": draw(st.booleans()),
    }
    if draw(st.booleans()):
        doc["tx_id"] = draw(refs(False))
    for f in ("channel", "merchant_category", "lat", "lon", "device_id", "session_id"):
        if draw(st.booleans()):
            doc[f] = draw(refs())
    if draw(st.booleans()):
        vals = draw(st.lists(texts, min_size=1, max_size=5, unique=True))
        cut = draw(st.integers(1, len(vals)))
        doc["label"] = {"source": draw(refs(False)), "fraud_values": vals[:cut], "legit_values": vals[cut:],
                        "other": draw(st.sampled_from(["none", "skip"]))}
    return doc


# Feature: helix-fraud-immune-system, Property 81: Column_Mapping document round-trip
@pytest.mark.property
@given(doc=mapping_docs(), fmt=st.sampled_from(["yaml", "json"]))
def test_mapping_document_round_trip(doc, fmt):
    """**Validates: Requirements 40.6**"""
    m = validate_mapping(doc)
    assert isinstance(m, ColumnMapping), m
    assert parse_mapping(dump_mapping(m, fmt), fmt) == m
