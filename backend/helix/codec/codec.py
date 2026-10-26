"""Event_Codec: parse JSON into TransactionEvent and format it back (Req 2.1-2.7, 2.9)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Union

from pydantic import ValidationError

from .models import TransactionEvent

ROOT = "$"  # field path for document-level errors (malformed JSON, non-object)


@dataclass(frozen=True, slots=True)
class FieldError:
    field: str  # dotted field path, or "$" for the whole document
    rule: str  # schema rule violated (Pydantic error type, e.g. "greater_than_equal")
    message: str


@dataclass(frozen=True, slots=True)
class CodecError:
    """Every schema violation in a rejected document, not just the first (Req 2.3)."""

    errors: tuple[FieldError, ...]

    @property
    def fields(self) -> frozenset[str]:
        return frozenset(e.field for e in self.errors)

    def __str__(self) -> str:
        return "; ".join(f"{e.field}: {e.rule} ({e.message})" for e in self.errors)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def parse(raw: Union[str, bytes, bytearray]) -> Union[TransactionEvent, CodecError]:
    """Parse one JSON document. Returns a TransactionEvent or a CodecError, never raises."""
    try:
        # parse_float=Decimal reads JSON numbers from their text, so 0.10 stays exact.
        doc = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, TypeError) as exc:  # JSONDecodeError and bad bytes are ValueErrors
        return CodecError((FieldError(ROOT, "json_invalid", str(exc)),))
    if not isinstance(doc, dict):
        return CodecError((FieldError(ROOT, "object_type", "document must be a JSON object"),))
    try:
        return TransactionEvent.model_validate(doc)
    except ValidationError as exc:
        return CodecError(
            tuple(
                FieldError(".".join(str(p) for p in e["loc"]) or ROOT, e["type"], e["msg"])
                for e in exc.errors()
            )
        )


def to_document(ev: TransactionEvent) -> dict[str, Any]:
    """JSON-ready dict: 2-digit amount string, ISO timestamp with offset, no missing fields."""
    doc = ev.model_dump(mode="python", exclude_none=True)
    doc["ts"] = ev.ts.isoformat()
    doc["amount"] = f"{ev.amount:.2f}"
    return doc


def format(ev: TransactionEvent) -> str:  # noqa: A001 - name fixed by the design
    """Format a TransactionEvent as a schema-conforming JSON document (Req 2.5)."""
    return json.dumps(to_document(ev), separators=(",", ":"), allow_nan=False)


parse_event = parse
format_event = format
