"""CSV_Adapter: parse and format CSV rows under a Column_Mapping (design §25, Req 40.2-40.5).

``MappedRowParser`` is a ``BoundRowCodec`` of the Dataset_Loader, so a generic CSV reuses the
loader's ordering, scope, external sort and label batching; ``MappingCodec`` binds it to a
file's header. The label column is returned beside the event (True = FRAUD, False = LEGIT,
None = no label), written by the loader to the Ground_Truth_Store only, and never placed on
the Transaction_Event (Req 40.4).

Row errors (``RowError(row, reason)``): ``wrong_column_count:<n>``, ``missing_column``,
``bad_timestamp``, ``bad_account``, ``bad_amount``, ``negative_amount``, ``amount_below_min``,
``amount_above_max``, ``unknown_currency``, ``bad_label``, ``bad_coordinate``, ``bad_<field>``
(a failed ``int`` transform on another field), ``missing_tx_id`` and ``invalid_event:<fields>``.
The checks run in the order of ``parse_row``, so the AMLworld mapping gives the same reasons.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Optional, Union

from pydantic import ValidationError

from helix.codec import TransactionEvent
from helix.codec.models import AMOUNT_MAX, AMOUNT_MIN, CENT

from .amlworld import ParsedRow, RowError, resolve_tz
from .mapping import ColumnMapping, ColumnRef, MappingError, header_errors

_DIGITS = re.compile(r"^[0-9]+$", re.ASCII)
_CCY = re.compile(r"^[A-Z]{3}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = Decimal(1_000_000)
_EVENT_OPTIONAL = (("channel", "channel"), ("merchant_category", "mcc"), ("device_id", "device_id"),
                   ("session_id", "session_id"))


class _Bad(Exception):
    """A value failed its transform; ``reason`` is the RowError reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


def _transform(raw: str, how: str) -> Optional[str]:
    if how == "none":
        return raw
    s = raw.strip()
    if how == "upper":
        return s.upper()
    if how == "int":
        return str(int(s)) if _DIGITS.match(s) else None
    return s


def _to_us(ts: datetime) -> int:
    d = ts - _EPOCH
    return (d.days * 86_400 + d.seconds) * 1_000_000 + d.microseconds


def _dec_str(v: Decimal) -> str:
    s = format(v, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


class MappedRowParser:
    """Parse / format one file's rows under a Column_Mapping."""

    def __init__(self, m: ColumnMapping, header: list[str], stem: str) -> None:
        self.m = m
        self.header = [h.strip() for h in header]
        self.width = len(header)
        self.stem = stem
        self.tz: tzinfo = resolve_tz(m.timestamp.tz)
        self._formats = (m.timestamp.format, *m.timestamp.alt_formats)
        self._names_inv = {v: k for k, v in m.currency_names.items()}

    # ------------------------------------------------------------------ helpers

    def _pos(self, ref: ColumnRef) -> Optional[int]:
        if ref.constant is not None:
            return None
        if ref.index is not None:
            return ref.index
        return self.header.index(str(ref.name).strip())

    def _get(self, ref: ColumnRef, fields: list[str], reason: str) -> str:
        pos = self._pos(ref)
        if pos is None:
            raw = str(ref.constant)
        elif pos >= len(fields):
            raise _Bad("missing_column")
        else:
            raw = fields[pos]
        v = _transform(raw, ref.transform)
        if v is None:
            raise _Bad(reason)
        return v

    def _ts_value(self, s: str) -> Optional[datetime]:
        s = s.strip()
        if not s:
            return None
        for fmt in self._formats:
            ts = self._ts_one(s, fmt)
            if ts is not None:
                return ts
        return None

    def _ts_one(self, s: str, fmt: str) -> Optional[datetime]:
        try:
            if fmt in ("epoch_s", "epoch_ms"):
                v = Decimal(s)
                if not v.is_finite():
                    return None
                us = int((v * (_US if fmt == "epoch_s" else Decimal(1000))).to_integral_value(ROUND_HALF_EVEN))
                return _EPOCH + timedelta(microseconds=us)
            if fmt == "iso8601":
                iso = s[:-1] + "+00:00" if s[-1] in "Zz" else s
                if len(iso) <= 10:  # a bare date is not a date-time
                    return None
                dt = datetime.fromisoformat(iso)
            else:
                dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self.tz)
            return dt.astimezone(timezone.utc)
        except (ValueError, InvalidOperation, OverflowError):
            return None

    def _account(self, refs: tuple[ColumnRef, ...], fields: list[str]) -> str:
        parts = [self._get(r, fields, "bad_account") for r in refs]
        if any(not p for p in parts):
            raise _Bad("bad_account")
        return self.m.join.join(parts)

    def _coord(self, ref: Optional[ColumnRef], fields: list[str], lim: float) -> Optional[float]:
        if ref is None:
            return None
        s = self._get(ref, fields, "bad_coordinate").strip()
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            raise _Bad("bad_coordinate") from None
        if not math.isfinite(v) or not -lim <= v <= lim:
            raise _Bad("bad_coordinate")
        return v

    # ------------------------------------------------------------------ BoundRowCodec

    def ts_of(self, fields: list[str]) -> Optional[datetime]:
        """Ordering key for the loader (None sorts last; ``parse`` then rejects the row)."""
        if len(fields) != self.width:
            return None
        try:
            return self._ts_value(self._get(self.m.timestamp.source, fields, "bad_timestamp"))
        except _Bad:
            return None

    def parse(self, fields: list[str], row: int) -> Union[ParsedRow, RowError]:
        if len(fields) != self.width:
            return RowError(row, f"wrong_column_count:{len(fields)}")
        m = self.m
        try:
            ts = self._ts_value(self._get(m.timestamp.source, fields, "bad_timestamp"))
            if ts is None:
                return RowError(row, "bad_timestamp")
            src = self._account(m.src_account, fields)
            dst = self._account(m.dst_account, fields)
            raw = self._get(m.amount.source, fields, "bad_amount").strip()
            if m.amount.thousands:
                raw = raw.replace(m.amount.thousands, "")
            if m.amount.decimal != ".":
                raw = raw.replace(m.amount.decimal, ".")
            try:
                amount = Decimal(raw)
                if not amount.is_finite():
                    return RowError(row, "bad_amount")
                amount = amount.quantize(CENT, rounding=ROUND_HALF_EVEN)  # huge exponents raise
            except InvalidOperation:
                return RowError(row, "bad_amount")
            if amount < 0:
                if m.amount.negative == "reject":
                    return RowError(row, "negative_amount")
                if m.amount.negative == "abs":
                    amount = -amount
            if amount < AMOUNT_MIN:
                return RowError(row, "amount_below_min")
            if amount > AMOUNT_MAX:
                return RowError(row, "amount_above_max")
            cur = self._get(m.currency, fields, "unknown_currency")
            code = m.currency_names.get(cur)
            if code is None:
                if m.currency_strict:
                    return RowError(row, "unknown_currency")
                code = cur
            if not _CCY.match(code):
                return RowError(row, "unknown_currency")
            label: Optional[bool] = None
            if m.label is not None:
                lv = self._get(m.label.source, fields, "bad_label")
                if lv in m.label.fraud_values:
                    label = True
                elif lv in m.label.legit_values:
                    label = False
                elif m.label.other == "skip":
                    return RowError(row, "bad_label")
            opt: dict[str, object] = {}
            for f, ev_field in _EVENT_OPTIONAL:
                ref = getattr(m, f)
                if ref is not None:
                    v = self._get(ref, fields, f"bad_{f}")
                    opt[ev_field] = v if v.strip() else None
            opt["lat"] = self._coord(m.lat, fields, 90.0)
            opt["lon"] = self._coord(m.lon, fields, 180.0)
            if m.tx_id is not None:
                tx_id = self._get(m.tx_id, fields, "missing_tx_id")
                if not tx_id.strip():
                    return RowError(row, "missing_tx_id")
            else:
                tx_id = f"{m.name}-{self.stem}-{row}"
        except _Bad as bad:
            return RowError(row, bad.reason)
        try:
            ev = TransactionEvent(tx_id=tx_id, ts=ts, src=src, dst=dst, amount=amount, currency=code, **opt)
        except ValidationError as exc:
            names = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
            return RowError(row, f"invalid_event:{','.join(names)}")
        return ev, label

    # ------------------------------------------------------------------ format (Req 40.5)

    def format_ts(self, ts: datetime) -> str:
        fmt = self.m.timestamp.format
        if fmt == "epoch_s":
            return _dec_str(Decimal(_to_us(ts)) / _US)
        if fmt == "epoch_ms":
            return _dec_str(Decimal(_to_us(ts)) / Decimal(1000))
        local = ts.astimezone(self.tz)
        return local.isoformat() if fmt == "iso8601" else local.strftime(fmt)

    def format(self, ev: TransactionEvent, label: Optional[bool]) -> list[str]:
        """Event (+ label) -> CSV fields in header order; inverse of ``parse`` for events
        representable under the mapping (fields map to columns, timestamp precision)."""
        m = self.m
        out = [""] * self.width

        def put(ref: Optional[ColumnRef], value: str) -> None:
            if ref is not None and (pos := self._pos(ref)) is not None:
                out[pos] = value

        if m.tx_id is not None:
            put(m.tx_id, ev.tx_id)
        put(m.timestamp.source, self.format_ts(ev.ts))
        for refs, acct in ((m.src_account, ev.src), (m.dst_account, ev.dst)):
            parts = acct.split(m.join, len(refs) - 1) if len(refs) > 1 else [acct]
            for r, p in zip(refs, parts):
                put(r, p)
        amt = format(ev.amount, "f")
        put(m.amount.source, amt.replace(".", m.amount.decimal) if m.amount.decimal != "." else amt)
        put(m.currency, self._names_inv.get(ev.currency, ev.currency))
        for f, ev_field in _EVENT_OPTIONAL:
            v = getattr(ev, ev_field)
            put(getattr(m, f), "" if v is None else str(v))
        put(m.lat, "" if ev.lat is None else repr(float(ev.lat)))
        put(m.lon, "" if ev.lon is None else repr(float(ev.lon)))
        if m.label is not None:
            lab = m.label
            put(lab.source, "" if label is None else (lab.fraud_values[0] if label else lab.legit_values[0]))
        return out


class MappingCodec:
    """``RowCodec`` of the Dataset_Loader for a Column_Mapping (``--dataset csv``)."""

    def __init__(self, m: ColumnMapping) -> None:
        self.mapping = m
        self.encoding = m.encoding
        self.delimiter = m.delimiter

    def bind(self, header: Optional[list[str]], stem: str, tz: tzinfo) -> Union[MappedRowParser, str]:
        errs: list[MappingError] = header_errors(self.mapping, header)
        if errs:
            return "; ".join(str(e) for e in errs)
        assert header is not None
        return MappedRowParser(self.mapping, header, stem)


__all__ = ["MappedRowParser", "MappingCodec"]
