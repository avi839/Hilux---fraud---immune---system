"""JSON conversion for API responses and WebSocket frames.

Amounts and priorities are decimal strings, timestamps ISO 8601 with offset, enums their
values, sets sorted lists (design "WebSocket messages").
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel

from helix.cases import Case
from helix.codec import TransactionEvent
from helix.codec.codec import to_document
from helix.explain import Explanation, ExplanationItem


def jsonable(o: Any) -> Any:
    """Recursively convert HELIX values (dataclasses, enums, Decimals, datetimes) to JSON."""
    if o is None or isinstance(o, (bool, int, str)):
        return o.value if isinstance(o, Enum) else o
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, Enum):
        return jsonable(o.value)
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, timedelta):
        return o.total_seconds()
    if isinstance(o, TransactionEvent):
        return to_document(o)
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: jsonable(getattr(o, f.name)) for f in dataclasses.fields(o)}
    if isinstance(o, Mapping):
        return {str(k.value if isinstance(k, Enum) else k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (set, frozenset)):
        return sorted((jsonable(x) for x in o), key=lambda x: (str(type(x)), str(x)))
    if isinstance(o, (list, tuple)):
        return [jsonable(x) for x in o]
    return str(o)


def dumps(o: Any) -> str:
    return json.dumps(jsonable(o), separators=(",", ":"), allow_nan=False)


# --- cases ---------------------------------------------------------------------------


def case_detail(summary: Mapping[str, Any], case: Case, has_chain: bool) -> dict[str, Any]:
    """Case summary (pipeline ``case_summary``) plus workflow fields and history."""
    out = dict(summary)
    out.update(
        seq_no=case.seq_no,
        tx_ids=sorted(case.tx_ids),
        created_at=case.created_at.isoformat(),
        risk_at_creation=case.risk_at_creation,
        escalated=case.escalated,
        escalated_by=case.escalated_by,
        opened_at=jsonable(case.opened_at),
        label_by=case.label_by,
        label_at=jsonable(case.label_at),
        label_note=case.label_note,
        closed_at=jsonable(case.closed_at),
        admitted_period=case.admitted_period,
        evidence_available=has_chain,
        history=[
            {"ts": h.ts.isoformat(), "action": h.action, "prev": h.prev, "new": h.new, "actor": h.actor}
            for h in case.history
        ],
    )
    return out


def item_doc(it: ExplanationItem) -> dict[str, Any]:
    d = jsonable(it)
    d["text"] = it.text
    return d


def explanation_doc(ex: Explanation) -> dict[str, Any]:
    return {
        "case_id": ex.case_id,
        "summary": ex.summary,
        "reasons": [item_doc(i) for i in ex.reasons],
        "mitigating": [item_doc(i) for i in ex.mitigating],
        "insufficient": [item_doc(i) for i in ex.insufficient],
        "notes": list(ex.notes),
        "message": ex.message,
        "text": ex.text,
    }


# --- page tokens (opaque base64 of the last sort key) ----------------------------------


def encode_cursor(seq_no: int) -> str:
    raw = json.dumps({"after": seq_no}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> Optional[int]:
    """Return the last sort key, or None when the token is not one we issued."""
    try:
        pad = "=" * (-len(token) % 4)
        doc = json.loads(base64.urlsafe_b64decode(token + pad).decode())
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    after = doc.get("after") if isinstance(doc, dict) else None
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        return None
    return after


def paginate(cases: Iterable[Case], page_size: int, after: Optional[int] = None) -> tuple[list[Case], Optional[str]]:
    """One page of Cases in ascending ``seq_no`` after the sort key ``after``, plus the next
    page token (None when no further Cases remain). Keyset pagination: Cases created between
    page requests appear on later pages, and no Case is returned twice."""
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    rest = sorted((c for c in cases if after is None or c.seq_no > after), key=lambda c: c.seq_no)
    page = rest[:page_size]
    nxt = encode_cursor(page[-1].seq_no) if len(rest) > page_size else None
    return page, nxt


__all__ = [
    "case_detail", "decode_cursor", "dumps", "encode_cursor", "explanation_doc", "jsonable", "paginate",
]
