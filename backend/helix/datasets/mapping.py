"""Column_Mapping: turn any bank CSV export into Transaction_Events (design §25, Req 40.1, 40.6).

A mapping assigns each Transaction_Event field a source: a header column ``name``, a 0-based
column ``index`` (covers duplicate headers such as AMLworld's two ``Account`` columns) or a
``constant``. ``validate_mapping`` reports every problem at once as ``MappingError(path,
reason)``: Pydantic checks the types and unknown keys (``extra="forbid"`` names the field),
then a walk over the raw document checks the rules Pydantic cannot express.

Extensions beyond the design sketch (needed so ``mappings/amlworld.yaml`` agrees with the
Dataset_Loader, Property 84): ``timestamp.alt_formats`` (further formats tried in order),
``amount.negative: below_min``, ``currency_strict`` (values must be in ``currency_names``)
and ``label.other: skip`` (a value in neither list skips the row as ``bad_label``).
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .amlworld import resolve_tz

TS_KEYWORDS: tuple[str, ...] = ("iso8601", "epoch_s", "epoch_ms")
OPTIONAL_REFS: tuple[str, ...] = ("channel", "merchant_category", "lat", "lon", "device_id", "session_id")
REQUIRED_SOURCES: tuple[str, ...] = ("timestamp", "src_account", "dst_account", "amount", "currency")
_CCY = re.compile(r"^[A-Z]{3}$")
_SAMPLE = datetime(2024, 3, 15, 13, 45, 30)  # strptime self-test value
_MODEL = ConfigDict(frozen=True, extra="forbid")


class ColumnRef(BaseModel):
    """One of ``name`` / ``index`` / ``constant``, then ``transform``."""

    model_config = _MODEL
    name: Optional[str] = None
    index: Optional[int] = Field(default=None, ge=0)
    constant: Optional[str] = None
    transform: Literal["none", "strip", "int", "upper"] = "strip"


class TimestampSpec(BaseModel):
    model_config = _MODEL
    source: ColumnRef
    format: str = "iso8601"  # "iso8601" | "epoch_s" | "epoch_ms" | strptime pattern
    alt_formats: tuple[str, ...] = ()  # tried in order after ``format``
    tz: str = "UTC"  # IANA name or fixed offset, applied to naive values


class AmountSpec(BaseModel):
    model_config = _MODEL
    source: ColumnRef
    decimal: str = "."
    thousands: str = ""
    negative: Literal["reject", "abs", "below_min"] = "reject"


class LabelSpec(BaseModel):
    model_config = _MODEL
    source: ColumnRef
    fraud_values: tuple[str, ...]
    legit_values: tuple[str, ...] = ()
    other: Literal["none", "skip"] = "none"  # value in neither list: no label, or skip the row


class ColumnMapping(BaseModel):
    model_config = _MODEL
    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=32)
    delimiter: str = ","
    encoding: str = "utf-8-sig"
    tx_id: Optional[ColumnRef] = None  # None -> f"{name}-{stem}-{row}"
    timestamp: TimestampSpec
    src_account: tuple[ColumnRef, ...]
    dst_account: tuple[ColumnRef, ...]
    join: str = ":"
    amount: AmountSpec
    currency: ColumnRef
    currency_names: dict[str, str] = {}  # e.g. "US Dollar" -> "USD"
    currency_strict: bool = False
    channel: Optional[ColumnRef] = None
    merchant_category: Optional[ColumnRef] = None
    lat: Optional[ColumnRef] = None
    lon: Optional[ColumnRef] = None
    device_id: Optional[ColumnRef] = None
    session_id: Optional[ColumnRef] = None
    label: Optional[LabelSpec] = None


@dataclass(frozen=True, slots=True)
class MappingError:
    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


# --------------------------------------------------------------------------- validation


def format_problem(fmt: Any) -> Optional[str]:
    """None when ``fmt`` is a timestamp keyword or a usable strptime pattern."""
    if fmt in TS_KEYWORDS:
        return None
    if not isinstance(fmt, str) or "%" not in fmt:
        return "must be iso8601, epoch_s, epoch_ms or a strptime pattern"
    try:
        back = datetime.strptime(_SAMPLE.strftime(fmt), fmt)
    except (ValueError, TypeError) as exc:
        return f"invalid strptime pattern: {exc}"
    if back.date() != _SAMPLE.date():
        return "strptime pattern must give year, month and day"
    return None


def _tz_problem(tz: Any) -> Optional[str]:
    if not isinstance(tz, str):
        return None  # Pydantic reports the type
    try:
        resolve_tz(tz)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError
        return f"unknown time zone {tz!r}"
    return None


def _ref_problems(path: str, v: Any) -> list[MappingError]:
    if not isinstance(v, Mapping):
        return []  # missing / wrong type: Pydantic reports it
    n = sum(v.get(k) is not None for k in ("name", "index", "constant"))
    if n == 0:
        return [MappingError(path, "no source: give one of name, index or constant")]
    if n > 1:
        return [MappingError(path, "give only one of name, index or constant")]
    return []


def _semantic(doc: Mapping[str, Any]) -> list[MappingError]:
    out: list[MappingError] = []
    get = doc.get
    d = get("delimiter", ",")
    if isinstance(d, str) and len(d) != 1:
        out.append(MappingError("delimiter", "must be a single character"))
    enc = get("encoding", "utf-8-sig")
    if isinstance(enc, str):
        try:
            codecs.lookup(enc)
        except LookupError:
            out.append(MappingError("encoding", f"unknown encoding {enc!r}"))
    if get("tx_id") is not None:
        out += _ref_problems("tx_id", get("tx_id"))
    ts = get("timestamp")
    if isinstance(ts, Mapping):
        if "source" in ts:
            out += _ref_problems("timestamp.source", ts["source"])
        if (p := format_problem(ts.get("format", "iso8601"))) is not None:
            out.append(MappingError("timestamp.format", p))
        alts = ts.get("alt_formats", ())
        if isinstance(alts, (list, tuple)):
            for i, f in enumerate(alts):
                if (p := format_problem(f)) is not None:
                    out.append(MappingError(f"timestamp.alt_formats.{i}", p))
        if (p := _tz_problem(ts.get("tz", "UTC"))) is not None:
            out.append(MappingError("timestamp.tz", p))
    for side in ("src_account", "dst_account"):
        refs = get(side)
        if isinstance(refs, (list, tuple)):
            if not refs:
                out.append(MappingError(side, "no source: give at least one column"))
            for i, r in enumerate(refs):
                out += _ref_problems(f"{side}.{i}", r)
    am = get("amount")
    if isinstance(am, Mapping):
        if "source" in am:
            out += _ref_problems("amount.source", am["source"])
        dec, th = am.get("decimal", "."), am.get("thousands", "")
        if isinstance(dec, str) and len(dec) != 1:
            out.append(MappingError("amount.decimal", "must be a single character"))
        elif isinstance(dec, str) and isinstance(th, str) and (len(th) > 1 or th == dec):
            out.append(MappingError("amount.thousands", "must be empty or one character other than decimal"))
    cur = get("currency")
    if cur is not None:
        out += _ref_problems("currency", cur)
        if isinstance(cur, Mapping) and isinstance(cur.get("constant"), str) and not _CCY.match(cur["constant"]):
            out.append(MappingError("currency.constant", "must be a 3-letter upper-case currency code"))
    names = get("currency_names")
    if isinstance(names, Mapping):
        for k, v in names.items():
            if isinstance(v, str) and not _CCY.match(v):
                out.append(MappingError(f"currency_names.{k}", "must be a 3-letter upper-case currency code"))
    for f in OPTIONAL_REFS:
        if get(f) is not None:
            out += _ref_problems(f, get(f))
    lab = get("label")
    if isinstance(lab, Mapping):
        if "source" in lab:
            out += _ref_problems("label.source", lab["source"])
        fv, lv = lab.get("fraud_values"), lab.get("legit_values", ())
        if isinstance(fv, (list, tuple)) and not fv:
            out.append(MappingError("label.fraud_values", "give at least one fraud value"))
        if isinstance(fv, (list, tuple)) and isinstance(lv, (list, tuple)) and set(map(str, fv)) & set(map(str, lv)):
            out.append(MappingError("label", "fraud_values and legit_values overlap"))
    return out


def _pydantic_errors(exc: ValidationError) -> list[MappingError]:
    out = []
    for e in exc.errors():
        path = ".".join(str(p) for p in e.get("loc", ())) or "$"
        kind = e.get("type")
        if kind == "missing":
            reason = "required: no source given"
        elif kind == "extra_forbidden":
            reason = "unknown field (not a Transaction_Event field or mapping option)"
        else:
            reason = str(e.get("msg"))
        out.append(MappingError(path, reason))
    return out


def validate_mapping(doc: Any) -> Union[ColumnMapping, list[MappingError]]:
    """The mapping, or every error found (Req 40.1)."""
    if not isinstance(doc, Mapping):
        return [MappingError("$", "a Column_Mapping must be an object")]
    errs: list[MappingError] = []
    model: Optional[ColumnMapping] = None
    try:
        model = ColumnMapping.model_validate(dict(doc))
    except ValidationError as exc:
        errs += _pydantic_errors(exc)
    errs += _semantic(doc)
    if errs:
        seen: set[tuple[str, str]] = set()
        return [e for e in errs if (e.path, e.reason) not in seen and not seen.add((e.path, e.reason))]
    assert model is not None
    return model


def parse_mapping(text: str, fmt: Literal["yaml", "json"]) -> Union[ColumnMapping, list[MappingError]]:
    try:
        doc = json.loads(text) if fmt == "json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        return [MappingError("$", f"unparseable {fmt} document: {exc}")]
    return validate_mapping(doc)


def load_mapping(path: Union[Path, str]) -> Union[ColumnMapping, list[MappingError]]:
    """Load a ``.yaml``/``.yml``/``.json`` Column_Mapping file; every error at once."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return [MappingError("$", f"{path}: file not found")]
    except (OSError, UnicodeDecodeError) as exc:
        return [MappingError("$", f"{path}: cannot read: {exc}")]
    return parse_mapping(text, "json" if path.suffix.lower() == ".json" else "yaml")


def dump_mapping(m: ColumnMapping, fmt: Literal["yaml", "json"]) -> str:
    data = m.model_dump(mode="json", exclude_none=True)
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


# --------------------------------------------------------------------------- header


def iter_refs(m: ColumnMapping) -> Iterator[tuple[str, ColumnRef]]:
    """Every ColumnRef of the mapping with its document path."""
    if m.tx_id is not None:
        yield "tx_id", m.tx_id
    yield "timestamp.source", m.timestamp.source
    for side in ("src_account", "dst_account"):
        for i, r in enumerate(getattr(m, side)):
            yield f"{side}.{i}", r
    yield "amount.source", m.amount.source
    yield "currency", m.currency
    for f in OPTIONAL_REFS:
        r = getattr(m, f)
        if r is not None:
            yield f, r
    if m.label is not None:
        yield "label.source", m.label.source


def header_errors(m: ColumnMapping, header: Optional[list[str]]) -> list[MappingError]:
    """Columns the mapping names that the CSV header does not have."""
    if header is None:
        return [MappingError("$", "empty file: no header row")]
    names = [h.strip() for h in header]
    out = []
    for path, r in iter_refs(m):
        if r.name is not None and r.name.strip() not in names:
            out.append(MappingError(f"{path}.name", f"column {r.name!r} is not in the header"))
        if r.index is not None and r.index >= len(names):
            out.append(MappingError(f"{path}.index", f"column index {r.index} is beyond the header ({len(names)} columns)"))
    return out


__all__ = [
    "AmountSpec", "ColumnMapping", "ColumnRef", "LabelSpec", "MappingError", "TimestampSpec",
    "dump_mapping", "format_problem", "header_errors", "iter_refs", "load_mapping", "parse_mapping",
    "validate_mapping",
]
