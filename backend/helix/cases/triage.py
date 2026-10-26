"""Auto_Triage: close idle LOW-tier Cases with Closure_Reason AUTO_CLEARED (design §26, Req 41.1-41.3).

Design decisions:

- Off by default (``triage.enabled``). The API state runs ``sweep`` between events (see the
  "Auto_Triage" block of ``helix.api.state.AppState``), never in the middle of one.
- Idle time is stream time (the SimClock): ``now - case.updated_at``. ``updated_at`` changes
  on every event, Motif, tier and workflow change, so a Case that is still growing is never
  idle. SLA_Timers stay wall-clock (Req 36.2).
- Eligible: tier LOW, status OPEN (IN_REVIEW Cases have been claimed by an Investigator), not
  escalated, not withheld, idle for at least ``auto_clear_after_s``, and no Canary reference.
  Canary Cases are excluded: a Canary touch is a high-confidence signal by construction
  (Req 33.3, 33.4), and the Prioritiser already exempts them from displacement, so clearing
  them silently would defeat the Canary.
- Closing goes through ``CaseManager.auto_clear``. Nothing here calls ``Helix.label``, so the
  feedback hooks (Feedback_Learner, Immune_System, Audit_Ledger FEEDBACK_LABEL, Indicator
  extraction) never see an AUTO_CLEARED closure (Req 41.2).
- With an Audit_Ledger, each closure appends ``CASE_AUTO_CLEARED`` with ``{case_id, idle_s}``
  and actor ``system`` (Req 41.3). The category is registered on the ledger on first use; an
  audit fault is logged and never stops the sweep.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Optional

from helix.cases.manager import CaseManager
from helix.cases.models import SYSTEM, Case, CaseStatus
from helix.config import SeverityTier
from helix.config.schema import TriageConfig

log = logging.getLogger(__name__)

AUDIT_CATEGORY = "CASE_AUTO_CLEARED"


def idle_s(case: Case, now: datetime) -> float:
    """Stream-time seconds since the Case was last updated."""
    return (now - case.updated_at).total_seconds()


class AutoTriage:
    def __init__(
        self,
        cfg: TriageConfig,
        cases: CaseManager,
        audit: Any = None,
        *,
        on_clear: Optional[Callable[[Case], None]] = None,
    ) -> None:
        self.cfg = cfg
        self.cases = cases
        self.audit = audit
        self.on_clear = on_clear  # e.g. the WorkloadMeter
        self.cleared_total = 0
        self.sweeps = 0
        self.audit_errors = 0

    def eligible(self, case: Case, now: datetime) -> bool:
        """The §26 eligibility predicate (Req 41.1)."""
        return (case.tier is SeverityTier.LOW
                and case.status is CaseStatus.OPEN
                and not case.escalated
                and case.case_id not in self.cases.canary_cases
                and not case.withheld
                and idle_s(case, now) >= float(self.cfg.auto_clear_after_s))

    def sweep(self, now: datetime) -> list[str]:
        """Close every eligible Case; returns their ids in ascending ``seq_no``."""
        self.sweeps += 1
        due = sorted((c for c in self.cases.cases.values() if self.eligible(c, now)), key=lambda c: c.seq_no)
        out: list[str] = []
        for case in due:
            idle = idle_s(case, now)
            self.cases.auto_clear(case.case_id, now)
            out.append(case.case_id)
            self.cleared_total += 1
            self._audit(case.case_id, idle, now)
            if self.on_clear is not None:
                try:
                    self.on_clear(case)
                except Exception:  # noqa: BLE001 - metrics must not stop the sweep
                    log.exception("auto-triage: on_clear hook failed for %s", case.case_id)
        return out

    def _audit(self, case_id: str, idle: float, now: datetime) -> None:
        ledger = self.audit
        if ledger is None:
            return
        try:
            cats = getattr(ledger, "categories", None)
            if isinstance(cats, frozenset) and AUDIT_CATEGORY not in cats:
                ledger.categories = cats | {AUDIT_CATEGORY}
            ledger.append(AUDIT_CATEGORY, SYSTEM, {"case_id": case_id, "idle_s": idle, "at": now.isoformat()})
        except Exception:  # noqa: BLE001 - audit faults are counted, never raised
            self.audit_errors += 1
            log.exception("auto-triage: audit append failed for %s", case_id)


__all__ = ["AUDIT_CATEGORY", "AutoTriage", "idle_s"]
