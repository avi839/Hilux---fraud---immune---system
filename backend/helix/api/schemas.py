"""Pydantic request and response models of the REST API (design §21).

Request models forbid unknown fields, so an undefined key is a 422 like any other
validation failure (Req 23.4).
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from helix.cases import MAX_NOTE_CHARS, Label

Actor = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


# --- errors ---------------------------------------------------------------------------


class PermittedRange(BaseModel):
    lo: float
    hi: float
    lo_open: bool = False
    rule: str


class FieldProblem(BaseModel):
    field: str = Field(description="dotted field path, '$' for the whole document")
    reason: str
    rule: Optional[str] = None
    permitted: Optional[PermittedRange] = Field(None, description="permitted range of a config setting")


class ValidationErrorBody(BaseModel):
    detail: str
    errors: list[FieldProblem]


class ErrorBody(_Open):
    detail: str
    case_id: Optional[str] = None


class ReplayRangeErrorBody(ValidationErrorBody):
    start: str
    end: str


# --- requests -------------------------------------------------------------------------


class OpenRequest(_Request):
    investigator: Actor


class FeedbackRequest(_Request):
    by: Actor
    label: Label
    note: str = Field("", max_length=MAX_NOTE_CHARS)


class EscalateRequest(_Request):
    by: Actor


CaseIdStr = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class BulkFeedbackRequest(_Request):
    """POST /cases/bulk-feedback (Req 41.6). The 1-50 distinct ids rule is checked by the
    handler, so the 422 body lists the offending ids (``BulkFailureBody``)."""

    by: Actor
    label: Label
    note: str = Field("", max_length=MAX_NOTE_CHARS)
    case_ids: list[CaseIdStr]


class BulkFailure(BaseModel):
    case_id: str
    reason: str
    status: int = Field(description="404, 409 or 422")


class BulkFailureBody(BaseModel):
    detail: str
    failures: list[BulkFailure]


class SuggestedLabelDoc(BaseModel):
    label: str = Field(description="CONFIRMED_FRAUD | FALSE_POSITIVE | NONE")
    confidence: float
    reason: str


class ExpandRequest(_Request):
    account: str = Field(min_length=1, max_length=64)
    t: Optional[AwareDatetime] = Field(None, description="expand in the replay frame at t instead of live")


class RollbackRequest(_Request):
    t: AwareDatetime


# --- responses ------------------------------------------------------------------------


class IngestAccepted(BaseModel):
    tx_id: str
    status: str = Field(description="accepted | duplicate")
    duplicate: bool
    backpressure: bool


class AmountAtRisk(BaseModel):
    currency: str
    amount: str


class CaseSummary(_Open):
    case_id: str
    status: str
    tier: str
    risk: float
    uncertainty: Optional[float] = None
    priority: str
    expected_loss: str
    placement: str
    amount_at_risk: list[AmountAtRisk]
    accounts: list[str]
    tx_count: int
    motifs: list[str]
    assignee: Optional[str] = None
    merged_into: Optional[str] = None
    label: Optional[str] = None
    updated_at: str
    closure_reason: Optional[str] = Field(None, description="LABELLED | MERGED | AUTO_CLEARED on CLOSED Cases")
    suggested_label: Optional[SuggestedLabelDoc] = Field(
        None, description="Label_Suggester advice; never stored, never applied (Req 41.4)")


class CaseDetail(CaseSummary):
    seq_no: int
    tx_ids: list[str]
    created_at: str
    closed_at: Optional[str] = None
    history: list[dict[str, Any]]


class BulkFeedbackResult(BaseModel):
    by: str
    label: str
    count: int
    case_ids: list[str]
    cases: list[CaseSummary]


class CasePage(BaseModel):
    items: list[CaseSummary]
    page_size: int
    total: int
    next_cursor: Optional[str] = Field(None, description="opaque token; null when no further Cases remain")
    backlog_count: int


class EvidenceResponse(BaseModel):
    case_id: str
    chain: Optional[dict[str, Any]] = Field(None, description="Evidence_Chain JSON (null before any evidence)")
    explanation: dict[str, Any]


class ConfigPatchResult(BaseModel):
    accepted: dict[str, Any]
    applies_from_period: int = Field(description="budget period index from which the change applies")
    pending: dict[str, Any]


class GeneratorState(BaseModel):
    running: bool
    changed: bool = Field(description="false when the generator was already in the requested state")


class RollbackResult(BaseModel):
    t: str
    records: list[dict[str, Any]]
    weights: dict[str, float]


class Document(_Open):
    """Free-form JSON object (subgraph, timeline, metrics, ...)."""


__all__ = [
    "AmountAtRisk",
    "BulkFailure",
    "BulkFailureBody",
    "BulkFeedbackRequest",
    "BulkFeedbackResult",
    "CaseIdStr",
    "SuggestedLabelDoc",
    "ConfigPatchResult",
    "GeneratorState",
    "PermittedRange",
    "RollbackResult",
    "CaseDetail",
    "CasePage",
    "CaseSummary",
    "Document",
    "ErrorBody",
    "EscalateRequest",
    "EvidenceResponse",
    "ExpandRequest",
    "FeedbackRequest",
    "FieldProblem",
    "IngestAccepted",
    "OpenRequest",
    "ReplayRangeErrorBody",
    "RollbackRequest",
    "ValidationErrorBody",
]
