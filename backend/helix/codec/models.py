"""TransactionEvent schema: the single source of the Transaction_Event format (Req 2.2)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

Id64 = Annotated[str, StringConstraints(min_length=1, max_length=64)]

AMOUNT_MIN = Decimal("0.01")
AMOUNT_MAX = Decimal("999999999.99")
CENT = Decimal("0.01")

Amount = Annotated[Decimal, Field(ge=AMOUNT_MIN, le=AMOUNT_MAX, decimal_places=2)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]

OPTIONAL_FIELDS: tuple[str, ...] = ("channel", "mcc", "lat", "lon", "device_id", "session_id")
REQUIRED_FIELDS: tuple[str, ...] = ("tx_id", "ts", "src", "dst", "amount", "currency")


class TransactionEvent(BaseModel):
    """Immutable transaction record. Missing and null Optional_Fields are both ``None``."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    tx_id: Id64
    ts: AwareDatetime
    src: Id64
    dst: Id64
    amount: Amount
    currency: Currency
    channel: Optional[Id64] = None
    mcc: Optional[Id64] = None
    lat: Optional[Latitude] = None
    lon: Optional[Longitude] = None
    device_id: Optional[Id64] = None
    session_id: Optional[Id64] = None

    @field_validator("ts", mode="before")
    @classmethod
    def _ts_must_be_iso_string(cls, v: Any) -> Any:
        # Reject epoch numbers; AwareDatetime then rejects naive strings.
        if isinstance(v, (str, datetime)):
            return v
        raise ValueError("timestamp must be an ISO 8601 date-time string with a UTC offset or Z")

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_number_or_string(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
            raise ValueError("amount must be a decimal number or numeric string")
        if isinstance(v, float):
            v = Decimal(repr(v))  # shortest repr, never the binary expansion
        return v

    @field_validator("amount", mode="after")
    @classmethod
    def _amount_quantise(cls, v: Decimal) -> Decimal:
        return v.quantize(CENT)  # exact: decimal_places=2 already rejected finer values

    @field_validator("lat", "lon", mode="before")
    @classmethod
    def _coordinate_is_number(cls, v: Any) -> Any:
        if v is None:
            return v
        if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
            raise ValueError("coordinate must be a number")
        return float(v)
