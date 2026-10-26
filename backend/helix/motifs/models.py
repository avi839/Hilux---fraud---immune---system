"""Motif data model (design "Graph, motifs and evidence chain", Requirement 11)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any

from helix.fusion import Mass
from helix.graph import Edge


class MotifType(str, Enum):
    ROUND_TRIP = "ROUND_TRIP_CYCLE"
    FAN_OUT = "FAN_OUT"
    FAN_IN = "FAN_IN"
    STRUCTURING = "STRUCTURING"
    MULE_CHAIN = "MULE_CHAIN"

    def __str__(self) -> str:  # StrEnum-like behaviour on Python 3.10
        return self.value


def motif_id_for(motif_type: MotifType, tx_ids: frozenset[str]) -> str:
    """Deterministic id: same (type, tx_id set) always yields the same id."""
    digest = hashlib.sha1("|".join(sorted(tx_ids)).encode()).hexdigest()[:16]
    return f"{motif_type.value}:{digest}"


_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class MotifInstance:
    motif_id: str
    motif_type: MotifType
    tx_ids: frozenset[str]  # dedup key with motif_type (Req 11.11)
    accounts: tuple[str, ...]  # path order for cycles and chains; (hub, *counterparties) for fans; (src,) for structuring
    ts_start: datetime
    ts_end: datetime
    mass: Mass
    truncated: bool
    # Additive to the design model: participating edges (path order for cycles/chains,
    # (ts, tx_id) order otherwise) and the type-specific listed fields of Req 11.2-11.5.
    edges: tuple[Edge, ...] = ()
    detail: Mapping[str, Any] = field(default=_EMPTY)

    @property
    def duration_s(self) -> float:
        return (self.ts_end - self.ts_start).total_seconds()


@dataclass(frozen=True, slots=True)
class MotifResult:
    instances: tuple[MotifInstance, ...]
    truncated: bool
