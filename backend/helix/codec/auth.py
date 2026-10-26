"""Auth_Event and Auth_Event_Codec (Req 28.2-28.4).

Design decisions:
- Mirrors the Transaction_Event codec: a frozen Pydantic model is the single schema,
  ``parse`` never raises and returns every field violation as a ``CodecError``, undefined
  keys are ignored, and ``format`` writes only present optional fields.
- Required: ``event_id``, ``ts`` (ISO 8601 with offset; epoch numbers and naive strings are
  malformed), ``account_id``, ``session_id``, ``event_type``. Optional: ``device_id``,
  ``ip``, ``lat``, ``lon``, ``payee_account_id``.
- ``payee_account_id`` is only meaningful for NEW_PAYEE_ADDED; on any other type it is an
  error on that field (strict), and it stays optional on NEW_PAYEE_ADDED (glossary).
- ``ip`` is validated with ``ipaddress`` and stored in canonical text form, so formatting
  then parsing gives an equal object (round-trip property, Req 28.4).
"""

from __future__ import annotations

import ipaddress
import json
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from .codec import ROOT, CodecError, FieldError, _reject_constant
from .models import Id64, Latitude, Longitude


class AuthEventType(str, Enum):
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    PASSWORD_RESET = "PASSWORD_RESET"
    MFA_SUCCESS = "MFA_SUCCESS"
    MFA_FAILURE = "MFA_FAILURE"
    NEW_PAYEE_ADDED = "NEW_PAYEE_ADDED"

    def __str__(self) -> str:
        return self.value


AUTH_REQUIRED_FIELDS: tuple[str, ...] = ("event_id", "ts", "account_id", "session_id", "event_type")
AUTH_OPTIONAL_FIELDS: tuple[str, ...] = ("device_id", "ip", "lat", "lon", "payee_account_id")


class AuthEvent(BaseModel):
    """Immutable authentication / account-security event. Missing and null optionals are ``None``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: Id64
    ts: AwareDatetime
    account_id: Id64
    session_id: Id64
    event_type: AuthEventType
    device_id: Optional[Id64] = None
    ip: Optional[str] = None
    lat: Optional[Latitude] = None
    lon: Optional[Longitude] = None
    payee_account_id: Optional[Id64] = None

    @field_validator("ts", mode="before")
    @classmethod
    def _ts_must_be_iso_string(cls, v: Any) -> Any:
        if isinstance(v, (str, datetime)):
            return v
        raise ValueError("timestamp must be an ISO 8601 date-time string with a UTC offset or Z")

    @field_validator("event_type", mode="before")
    @classmethod
    def _event_type_is_string(cls, v: Any) -> Any:
        if isinstance(v, AuthEventType) or (isinstance(v, str) and v in AuthEventType.__members__):
            return v
        raise ValueError(f"unknown event type {v!r}; expected one of {', '.join(AuthEventType.__members__)}")

    @field_validator("ip", mode="before")
    @classmethod
    def _ip_address(cls, v: Any) -> Any:
        if v is None:
            return v
        if not isinstance(v, str):
            raise ValueError("ip must be an IPv4 or IPv6 address string")
        try:
            return str(ipaddress.ip_address(v))  # canonical form
        except ValueError:
            raise ValueError(f"not an IPv4 or IPv6 address: {v!r}") from None

    @field_validator("lat", "lon", mode="before")
    @classmethod
    def _coordinate_is_number(cls, v: Any) -> Any:
        if v is None:
            return v
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("coordinate must be a number")
        return float(v)

    @model_validator(mode="after")
    def _payee_only_for_new_payee(self) -> AuthEvent:
        if self.payee_account_id is not None and self.event_type is not AuthEventType.NEW_PAYEE_ADDED:
            raise ValueError("payee_account_id is only allowed for NEW_PAYEE_ADDED")
        return self


def _errors(exc: ValidationError) -> CodecError:
    out = []
    for e in exc.errors():
        path = ".".join(str(p) for p in e["loc"])
        if not path and "payee_account_id" in str(e.get("msg", "")):
            path = "payee_account_id"  # model-level rule: blame the offending field
        out.append(FieldError(path or ROOT, e["type"], e["msg"]))
    return CodecError(tuple(out))


def parse_auth(raw: Union[str, bytes, bytearray]) -> Union[AuthEvent, CodecError]:
    """Parse one Auth_Event JSON document. Returns an AuthEvent or a CodecError, never raises."""
    try:
        doc = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, TypeError) as exc:
        return CodecError((FieldError(ROOT, "json_invalid", str(exc)),))
    if not isinstance(doc, dict):
        return CodecError((FieldError(ROOT, "object_type", "document must be a JSON object"),))
    return validate_auth(doc)


def validate_auth(doc: dict[str, Any]) -> Union[AuthEvent, CodecError]:
    try:
        return AuthEvent.model_validate(doc)
    except ValidationError as exc:
        return _errors(exc)


def auth_to_document(ev: AuthEvent) -> dict[str, Any]:
    doc = ev.model_dump(mode="python", exclude_none=True)
    doc["ts"] = ev.ts.isoformat()
    doc["event_type"] = ev.event_type.value
    return doc


def format_auth(ev: AuthEvent) -> str:
    """Format an AuthEvent as a schema-conforming JSON document (Req 28.4)."""
    return json.dumps(auth_to_document(ev), separators=(",", ":"), allow_nan=False)


def event_id_of(ev: Any) -> str:
    """Identifier of a Transaction_Event (``tx_id``) or Auth_Event (``event_id``)."""
    return ev.event_id if isinstance(ev, AuthEvent) else ev.tx_id


__all__ = [
    "AUTH_OPTIONAL_FIELDS",
    "AUTH_REQUIRED_FIELDS",
    "AuthEvent",
    "AuthEventType",
    "auth_to_document",
    "event_id_of",
    "format_auth",
    "parse_auth",
    "validate_auth",
]
