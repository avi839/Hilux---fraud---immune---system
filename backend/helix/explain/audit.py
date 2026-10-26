"""Audit_Ledger hook point for Case decisions (Req 18.9).

The Audit_Ledger itself is Increment 4 (Req 35). Until then the hook is a no-op: the
Case_Manager (or API layer) calls ``audit_decision`` for each escalation or stored
Feedback_Label and the record is handed to ``noop_audit_hook``. Increment 4 swaps in a
ledger writer with the same ``AuditHook`` signature.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from helix.explain.chain import EvidenceChain, dumps

AUDITED_ACTIONS = frozenset({"escalate", "label"})


@dataclass(frozen=True, slots=True)
class AuditRecord:
    case_id: str
    ts: datetime
    action: str  # "escalate" | "label"
    actor: str
    prev: Any
    new: Any
    note: str | None
    evidence_digest: str | None  # sha256 of the canonical Evidence_Chain JSON


AuditHook = Callable[[AuditRecord], None]


def noop_audit_hook(record: AuditRecord) -> None:
    """Placeholder until the Audit_Ledger exists (Increment 4)."""
    return None


def evidence_digest(chain: EvidenceChain) -> str:
    return hashlib.sha256(dumps(chain).encode("utf-8")).hexdigest()


def audit_decision(
    entry: Any,
    chain: EvidenceChain | None = None,
    *,
    note: str | None = None,
    hook: AuditHook = noop_audit_hook,
) -> AuditRecord | None:
    """Forward an escalation or label ``CaseHistoryEntry`` to the Audit_Ledger hook.

    Returns the record handed to the hook, or None for actions that are not audited.
    """
    if entry.action not in AUDITED_ACTIONS:
        return None
    rec = AuditRecord(entry.case_id, entry.ts, entry.action, entry.actor, entry.prev, entry.new, note,
                      None if chain is None else evidence_digest(chain))
    hook(rec)
    return rec
