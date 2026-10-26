"""Ground-truth record and writer protocol shared by label producers.

Producers (generator, attacker, loader) only ever *write* labels. This module
deliberately imports nothing from `helix.evaluation`, so writing a label never
pulls the reader side of the Ground_Truth_Store into the detection path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Optional, Protocol, runtime_checkable

GTLabel = Literal["FRAUD", "LEGIT", "LEGIT_CHANGE"]
ChangeKind = Literal["relocation", "salary", "new_payee", "travel"]


class GTSource(str, Enum):
    ATTACKER = "ATTACKER"
    GENERATOR = "GENERATOR"
    LOADER = "LOADER"


@dataclass(frozen=True, slots=True, kw_only=True)
class GroundTruthRecord:
    event_id: str  # tx_id, or account id for behaviour changes
    label: GTLabel
    source: GTSource
    written_at: datetime
    campaign_id: Optional[str] = None
    genome_id: Optional[str] = None
    parent_genome_id: Optional[str] = None
    change_kind: Optional[ChangeKind] = None
    pattern_id: Optional[str] = None
    pattern_type: Optional[str] = None
    # Behaviour-change window [effective_at, effective_until); until=None means permanent.
    effective_at: Optional[datetime] = None
    effective_until: Optional[datetime] = None
    # HELIX_Technique tags of the Pattern_Genome (Req 30.5); only filled with soc.enabled.
    # (Not named ``techniques``: GroundTruthRecord field names are banned in Detection_Rules.)
    technique_tags: tuple[str, ...] = ()


@runtime_checkable
class GroundTruthWriter(Protocol):
    def write(self, rec: GroundTruthRecord) -> None: ...


class InMemoryGroundTruth:
    """List-backed writer for tests and demos without Storage."""

    def __init__(self) -> None:
        self.records: list[GroundTruthRecord] = []

    def write(self, rec: GroundTruthRecord) -> None:
        self.records.append(rec)
