"""Bulk feedback validation (design §26, Req 41.6, 41.7): phase 1 of ``Helix.label_bulk``.

Validation never mutates anything. A request fails when it names 0 or more than
``MAX_BULK_CASES`` ids, a duplicate id (422), an unknown Case (404), a CLOSED Case, or an
IN_REVIEW / assigned Case whose assignee is not the requester (409). An OPEN unassigned Case
is valid and is treated as opened by the requester. Every failing id is listed (one entry
per id, with its most severe reason) and the response status is the most severe one, in the
order 422 > 404 > 409. The Requirement 18 rules applied here are the same ones
``CaseManager.open`` / ``submit_label`` enforce, so phase 2 cannot fail on a validated batch.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Optional

from helix.cases.manager import CaseManager
from helix.cases.models import MAX_NOTE_CHARS, CaseStatus, Label

MAX_BULK_CASES = 50
_SEVERITY = {422: 3, 404: 2, 409: 1}


@dataclass(frozen=True, slots=True)
class BulkFailure:
    case_id: str
    reason: str
    status: Literal[404, 409, 422]

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "reason": self.reason, "status": self.status}


class BulkFeedbackError(Exception):
    """A rejected bulk feedback request; nothing was changed."""

    def __init__(self, status: int, failures: Sequence[BulkFailure], detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.failures = list(failures)
        self.detail = detail


def validate_bulk(cases: CaseManager, by: str, label: Any, note: str,
                  case_ids: Sequence[str]) -> Optional[BulkFeedbackError]:
    """Phase 1: the rejection for this request, or None when every Case passes."""
    ids = [str(c) for c in case_ids]
    distinct = list(dict.fromkeys(ids))
    if not ids:
        return BulkFeedbackError(422, [], "at least 1 case id is required")
    req: Optional[str] = None
    if not isinstance(by, str) or not by.strip():
        req = "investigator id must be a non-empty string"
    elif not isinstance(note, str) or len(note) > MAX_NOTE_CHARS:
        req = f"note must be a string of at most {MAX_NOTE_CHARS} characters"
    else:
        try:
            Label(label)
        except ValueError:
            req = f"label must be CONFIRMED_FRAUD or FALSE_POSITIVE, got {label!r}"
    if req is None and len(ids) > MAX_BULK_CASES:
        req = f"{len(ids)} case ids given; at most {MAX_BULK_CASES} are allowed"
    if req is not None:  # the whole request is malformed: every id fails
        return BulkFeedbackError(422, [BulkFailure(c, req, 422) for c in distinct], req)

    found: dict[str, BulkFailure] = {}
    for cid, n in Counter(ids).items():
        if n > 1:
            found[cid] = BulkFailure(cid, f"duplicate case id ({n} times)", 422)
    for cid in distinct:
        if cid in found:
            continue
        case = cases.cases.get(cid)
        if case is None:
            found[cid] = BulkFailure(cid, "no such case", 404)
        elif case.status is CaseStatus.CLOSED:
            found[cid] = BulkFailure(cid, "case is CLOSED", 409)
        elif case.assignee is not None and case.assignee != by:
            found[cid] = BulkFailure(cid, f"case is assigned to {case.assignee}", 409)
    if not found:
        return None
    failures = [found[c] for c in distinct if c in found]
    status = max((f.status for f in failures), key=_SEVERITY.__getitem__)
    return BulkFeedbackError(status, failures, f"{len(failures)} of {len(distinct)} cases failed validation; "
                                               "nothing was changed")


__all__ = ["MAX_BULK_CASES", "BulkFailure", "BulkFeedbackError", "validate_bulk"]
