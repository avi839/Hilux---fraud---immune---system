"""AMLworld currency names -> 3-letter codes (design §3a).

AMLworld writes currency *names*, not ISO codes. The table below covers the 15 names
seen in the published HI/LI files. Bitcoin has no ISO code; it uses the common
unofficial `XBT`. An unknown name makes a row unparseable (`unknown_currency`).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Optional

NAME_TO_CODE: Mapping[str, str] = MappingProxyType(
    {
        "US Dollar": "USD",
        "Euro": "EUR",
        "Yuan": "CNY",
        "Yen": "JPY",
        "UK Pound": "GBP",
        "Rupee": "INR",
        "Ruble": "RUB",
        "Australian Dollar": "AUD",
        "Canadian Dollar": "CAD",
        "Swiss Franc": "CHF",
        "Mexican Peso": "MXN",
        "Brazil Real": "BRL",
        "Saudi Riyal": "SAR",
        "Shekel": "ILS",
        "Bitcoin": "XBT",
    }
)

CODE_TO_NAME: Mapping[str, str] = MappingProxyType({v: k for k, v in NAME_TO_CODE.items()})
assert len(CODE_TO_NAME) == len(NAME_TO_CODE), "currency table must be a bijection"


def code_for(name: str) -> Optional[str]:
    return NAME_TO_CODE.get(name.strip())


def name_for(code: str) -> str:
    """Inverse lookup used by `format_row`; raises KeyError for codes AMLworld never uses."""
    return CODE_TO_NAME[code]
