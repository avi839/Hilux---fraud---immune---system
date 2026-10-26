"""Case data model (design "Cases", Requirements 12, 13, 14, 18)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Union

from helix.config import KillChainStage, SeverityTier
from helix.fusion import FusedAssessment
from helix.motifs import MotifInstance
from helix.risk import CaseRisk

Scalar = Union[str, int, float, bool, None]

MAX_NOTE_CHARS = 2_000
SYSTEM = "system"


class CaseStatus(str, Enum):
    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    CLOSED = "CLOSED"

    def __str__(self) -> str:
        return self.value


class Label(str, Enum):
    CONFIRMED_FRAUD = "CONFIRMED_FRAUD"
    FALSE_POSITIVE = "FALSE_POSITIVE"

    def __str__(self) -> str:
        return self.value


class ClosureReason(str, Enum):
    """Why a Case is CLOSED (Req 41.1). AUTO_CLEARED is a closure reason, never a Feedback_Label:
    it never reaches the Feedback_Learner, the Immune_System or the indicator hooks."""

    LABELLED = "LABELLED"
    MERGED = "MERGED"
    AUTO_CLEARED = "AUTO_CLEARED"

    def __str__(self) -> str:
        return self.value


class Placement(str, Enum):
    ACTIVE = "ACTIVE"
    BACKLOG = "BACKLOG"
    NONE = "NONE"

    def __str__(self) -> str:
        return self.value


# --- errors -------------------------------------------------------------------


class CaseError(Exception):
    """Base class for Case_Manager request errors; the case is left unchanged."""

    def __init__(self, case_id: str, reason: str) -> None:
        super().__init__(f"{case_id}: {reason}")
        self.case_id = case_id
        self.reason = reason


class CaseNotFound(CaseError):
    """No Case with this id (API: 404)."""


class CaseConflict(CaseError):
    """Wrong status or wrong assignee (API: 409)."""

    def __init__(self, case_id: str, reason: str, assignee: Optional[str] = None) -> None:
        super().__init__(case_id, reason)
        self.assignee = assignee


class CaseValidationError(CaseError):
    """Malformed request: bad label or note too long (API: 422)."""


# --- records --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseHistoryEntry:  # append-only
    case_id: str
    ts: datetime
    action: str  # create | add | merge | open | label | escalate | tier | placement | auto_clear
    prev: Scalar
    new: Scalar
    actor: str  # "system" or investigator id


@dataclass(frozen=True, slots=True)
class CaseChange:
    """Change notification for the pipeline to publish (Req 13.10)."""

    seq: int  # monotone per CaseManager
    ts: datetime
    case_id: str
    action: str  # create | add | merge | merged | tier | placement | priority | open | label | escalate
    status: CaseStatus
    tier: SeverityTier
    risk: float
    priority: Decimal
    placement: Placement
    merged_into: Optional[str] = None
    merged_from: tuple[str, ...] = ()


@dataclass(slots=True)
class Case:
    case_id: str  # "C-<seq_no>"
    seq_no: int
    status: CaseStatus
    tier: SeverityTier
    risk: float  # [0, 1]
    risk_at_creation: float  # Req 21.2 low-risk confirmed-fraud trigger
    expected_loss: Decimal
    priority: Decimal  # expected_loss - Investigation_Cost
    accounts: set[str]
    tx_ids: set[str]
    assignee: Optional[str]
    created_at: datetime
    updated_at: datetime
    admitted_period: Optional[int]  # budget period index when admitted to the Active_Queue
    placement: Placement
    merged_into: Optional[str]  # set only on CLOSED non-survivors
    label: Optional[Label]
    label_note: Optional[str]  # note <= 2000 chars
    escalated: bool  # sticky CRITICAL
    history: list[CaseHistoryEntry]
    # Contributor references for the Evidence_Chain (task 7.2) and risk recompute.
    assessments: dict[str, FusedAssessment] = field(default_factory=dict)  # tx_id -> stage-1 assessment
    amounts: dict[str, Decimal] = field(default_factory=dict)  # tx_id -> amount at risk
    motifs: dict[str, MotifInstance] = field(default_factory=dict)  # motif_id -> attached instance
    case_risk: Optional[CaseRisk] = None  # last recompute (stage-2 assessment when Motifs are attached)
    opened_at: Optional[datetime] = None
    label_by: Optional[str] = None
    label_at: Optional[datetime] = None
    escalated_at: Optional[datetime] = None
    escalated_by: Optional[str] = None
    withheld: bool = False  # ProfileStore.withhold already called (first reach of HIGH)
    update_no: int = 0  # global touch counter: breaks updated_at ties for "most recently updated"
    closure_reason: Optional[ClosureReason] = None  # set on every CLOSED Case (Req 41.1)
    closed_at: Optional[datetime] = None  # stream time of the closure
    # Kill chain (Req 29, only maintained when soc.enabled): the most advanced stage of the
    # Evidence_Chain, the reconstructed KillChain, and Expected_Loss before the multiplier.
    kill_chain_stage: Optional[KillChainStage] = None
    kill_chain: Optional[Any] = None  # helix.cases.killchain.KillChain
    expected_loss_base: Optional[Decimal] = None
    # Incremental aggregates kept by the Case_Manager so a recompute costs O(1) in the Case
    # size (task 19.1, Req 25). Each cache records how many items it covers; a mismatch with
    # the live dict (e.g. a Case restored from Storage) triggers one full rescan.
    amount_total: Decimal = Decimal("0")  # sum(amounts.values())
    amount_n: int = -1  # len(amounts) covered by amount_total; -1 = rescan
    best_fa: Optional[FusedAssessment] = None  # risk.best_assessment(assessments.values())
    best_n: int = -1  # len(assessments) covered by best_fa; -1 = rescan
    stage2_sel: dict[Any, tuple[str, MotifInstance]] = field(default_factory=dict)  # MotifType -> best
    stage2_n: int = -1  # len(motifs) covered by stage2_sel; -1 = rescan

    @property
    def is_open(self) -> bool:
        return self.status is not CaseStatus.CLOSED


def case_id_for(seq_no: int) -> str:
    return f"C-{seq_no}"


def seq_of(case_id: str) -> int:
    """Numeric part of ``C-<seq_no>``, so Case IDs compare numerically."""
    return int(case_id.split("-", 1)[1])
