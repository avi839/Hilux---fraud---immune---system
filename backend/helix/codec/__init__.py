"""HELIX codec package: Event_Codec (Transaction_Event) and Auth_Event_Codec JSON parse/format."""

from .auth import (
    AUTH_OPTIONAL_FIELDS,
    AUTH_REQUIRED_FIELDS,
    AuthEvent,
    AuthEventType,
    auth_to_document,
    event_id_of,
    format_auth,
    parse_auth,
    validate_auth,
)
from .codec import ROOT, CodecError, FieldError, format, format_event, parse, parse_event, to_document
from .models import OPTIONAL_FIELDS, REQUIRED_FIELDS, Id64, TransactionEvent

__all__ = [
    "AUTH_OPTIONAL_FIELDS",
    "AUTH_REQUIRED_FIELDS",
    "ROOT",
    "AuthEvent",
    "AuthEventType",
    "CodecError",
    "FieldError",
    "Id64",
    "OPTIONAL_FIELDS",
    "REQUIRED_FIELDS",
    "TransactionEvent",
    "auth_to_document",
    "event_id_of",
    "format",
    "format_auth",
    "format_event",
    "parse",
    "parse_auth",
    "parse_event",
    "to_document",
    "validate_auth",
]
