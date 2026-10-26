"""Prioritiser: alert-budgeted Active_Queue and Backlog (design §12, Requirement 14).

- ``active`` and ``backlog`` are ``SortedList``s of ``(-priority, created_at, seq_no, case_id)``,
  i.e. Case_Priority descending, then creation time ascending, then Case number.
- Budget periods are ``floor((now - start) / period)`` on the ``SimClock``; period 0 starts
  when the Prioritiser is built (HELIX startup).
- ``admitted_now`` counts Cases admitted to the Active_Queue in the current period minus
  those displaced to the Backlog in the same period. Closing a Case (label, merge) does not
  give its slot back: the budget is admissions per period, not queue size (Req 14.4).
- Every placement change is recorded as a ``Move``; the Case_Manager drains them to keep
  ``Case.placement`` in sync and publish updates.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Union

from sortedcontainers import SortedList

from helix.cases.models import Placement
from helix.config import Range
from helix.config.schema import PrioritiserConfig
from helix.sim.clock import SimClock

Key = tuple[Decimal, datetime, int, str]

ALERT_BUDGET = "alert_budget"
INVESTIGATION_COST = "investigation_cost"
_CHANGEABLE = (ALERT_BUDGET, INVESTIGATION_COST)


def _range(name: str) -> Range:
    for m in PrioritiserConfig.model_fields[name].metadata:
        if isinstance(m, Range):
            return m
    raise LookupError(name)  # pragma: no cover - schema always declares it


def to_decimal(x: Union[int, float, Decimal]) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(repr(float(x))) if isinstance(x, float) else Decimal(x)


class PrioritiserChangeError(ValueError):
    """Out-of-range Alert_Budget / Investigation_Cost request (Req 14.11)."""

    def __init__(self, name: str, value: object, rule: str) -> None:
        super().__init__(f"prioritiser.{name}={value!r}: {rule}")
        self.name = name
        self.value = value
        self.rule = rule


@dataclass(frozen=True, slots=True)
class CaseKey:
    case_id: str
    seq_no: int
    created_at: datetime
    expected_loss: Decimal
    opened: bool = False  # opened by an Investigator: never displaced
    # References a Canary (Req 33.4, 14.14): always admitted, outside the Alert_Budget count,
    # never displaced or moved to the Backlog. Only ever True when soc.enabled.
    canary: bool = False


@dataclass(frozen=True, slots=True)
class Move:
    case_id: str
    old: Placement
    new: Placement
    period: int
    ts: datetime
    reason: str  # admit | displace | backlog | refill | remove | nonpositive


@dataclass(frozen=True, slots=True)
class SettingChange:
    name: str
    old: float
    new: float
    ts: datetime
    period: int


@dataclass(frozen=True, slots=True)
class PeriodReport:
    """Evaluation hook payload; the Evaluation_Monitor (task 9.3) adds ground-truth fields."""

    period: int
    start: datetime
    end: datetime
    admitted: int
    backlog_size: int
    active_size: int


@dataclass(slots=True)
class _Entry:
    key: CaseKey
    priority: Decimal
    placement: Placement
    admitted_period: Optional[int]
    sort: Key
    counted: bool = False  # its admission counts against the Alert_Budget (False for Canary Cases)


class Prioritiser:
    def __init__(self, cfg: PrioritiserConfig, clock: SimClock, start: Optional[datetime] = None) -> None:
        self.clock = clock
        self.start = start if start is not None else clock.now()
        self.period = timedelta(seconds=cfg.period_s)
        self.alert_budget: int = int(cfg.alert_budget)
        self.investigation_cost: Decimal = to_decimal(cfg.investigation_cost)
        self.current_period = 0
        self.admitted_now = 0
        self.active: SortedList = SortedList()
        self.backlog: SortedList = SortedList()
        self._entries: dict[str, _Entry] = {}
        self._pending: dict[str, Union[int, float]] = {}
        self.change_log: list[SettingChange] = []  # for Storage (config_changes, Req 14.10)
        self.period_hooks: list[Callable[[PeriodReport], None]] = []  # Evaluation_Monitor placeholder
        self._moves: list[Move] = []

    # --- queries ---------------------------------------------------------------

    def period_index(self, now: datetime) -> int:
        return max(0, math.floor((now - self.start) / self.period))

    def priority_for(self, expected_loss: Decimal) -> Decimal:
        return expected_loss - self.investigation_cost

    def placement_of(self, case_id: str) -> Placement:
        e = self._entries.get(case_id)
        return e.placement if e else Placement.NONE

    def priority_of(self, case_id: str) -> Optional[Decimal]:
        e = self._entries.get(case_id)
        return e.priority if e else None

    def admitted_period_of(self, case_id: str) -> Optional[int]:
        e = self._entries.get(case_id)
        return e.admitted_period if e else None

    def active_ids(self) -> list[str]:
        return [k[3] for k in self.active]

    def backlog_ids(self) -> list[str]:
        return [k[3] for k in self.backlog]

    def pending(self) -> dict[str, Union[int, float]]:
        return dict(self._pending)

    def drain_moves(self) -> list[Move]:
        out, self._moves = self._moves, []
        return out

    # --- mutations ---------------------------------------------------------------

    def upsert(self, case: CaseKey) -> Placement:
        """Insert or re-rank a Case; returns its placement after admission (Req 14.5-14.8)."""
        self.on_tick(self.clock.now())
        prio = self.priority_for(case.expected_loss)
        sort: Key = (-prio, case.created_at, case.seq_no, case.case_id)
        e = self._entries.get(case.case_id)
        if e is None:
            e = _Entry(case, prio, Placement.NONE, None, sort)
            self._entries[case.case_id] = e
        else:
            self._detach(e)
            e.key, e.priority, e.sort = case, prio, sort

        if e.placement is Placement.ACTIVE:
            if case.canary:  # Req 33.4: stays Active and stops counting against the budget
                if e.counted and e.admitted_period == self.current_period:
                    self.admitted_now -= 1
                e.counted = False
                self.active.add(e.sort)
            elif prio <= 0:  # Req 14.8: an Active Case whose priority falls to <= 0 moves to the Backlog
                self._to_backlog(e, "nonpositive")
            else:
                self.active.add(e.sort)
            return e.placement
        if case.canary:  # Req 33.4: admitted regardless of budget and priority, not counted
            self._admit(e, "canary", counted=False)
            return e.placement
        self._admit_or_backlog(e)
        return e.placement

    def mark_opened(self, case_id: str) -> None:
        """Record that an Investigator opened the Case; placement and order are unchanged."""
        e = self._entries.get(case_id)
        if e is not None and not e.key.opened:
            k = e.key
            e.key = CaseKey(k.case_id, k.seq_no, k.created_at, k.expected_loss, opened=True, canary=k.canary)

    def remove(self, case_id: str) -> None:
        """Drop a Case from both queues (CLOSED by label or merge). Its admission still counts."""
        e = self._entries.pop(case_id, None)
        if e is None:
            return
        self._detach(e)
        if e.placement is not Placement.NONE:
            self._move(e, Placement.NONE, "remove")

    def request_change(self, name: str, value: Union[int, float]) -> None:
        """Queue an Alert_Budget / Investigation_Cost change for the next period (Req 14.10, 14.11)."""
        if name not in _CHANGEABLE:
            raise PrioritiserChangeError(name, value, f"must be one of {', '.join(_CHANGEABLE)}")
        rng = _range(name)
        ok_type = isinstance(value, (int, float)) and not isinstance(value, bool)
        if name == ALERT_BUDGET and ok_type and isinstance(value, float):
            ok_type = value.is_integer()
        if not ok_type or not rng.contains(float(value)):
            rule = rng.describe() + (" and be an integer" if name == ALERT_BUDGET else "")
            raise PrioritiserChangeError(name, value, rule)
        self._pending[name] = int(value) if name == ALERT_BUDGET else float(value)

    def on_tick(self, now: datetime) -> None:
        """Roll to the period containing ``now``: apply pending changes, reset, refill (Req 14.9)."""
        p = self.period_index(now)
        if p <= self.current_period:
            return
        report = PeriodReport(
            period=self.current_period,
            start=self.start + self.period * self.current_period,
            end=self.start + self.period * (self.current_period + 1),
            admitted=self.admitted_now,
            backlog_size=len(self.backlog),
            active_size=len(self.active),
        )
        for hook in list(self.period_hooks):
            hook(report)

        self.current_period = p
        self.admitted_now = 0
        cost_changed = self._apply_pending(now)
        if cost_changed:
            self._rekey_all()
        for sort in list(self.backlog):
            if self.admitted_now >= self.alert_budget:
                break
            e = self._entries[sort[3]]
            if e.priority <= 0:
                break  # key order: every later Backlog Case also has priority <= 0
            self.backlog.remove(sort)
            self._admit(e, "refill")

    # --- internals -------------------------------------------------------------------

    def _apply_pending(self, now: datetime) -> bool:
        cost_changed = False
        for name in _CHANGEABLE:
            if name not in self._pending:
                continue
            new = self._pending.pop(name)
            if name == ALERT_BUDGET:
                old: float = self.alert_budget
                self.alert_budget = int(new)
            else:
                old = float(self.investigation_cost)
                self.investigation_cost = to_decimal(float(new))
                cost_changed = cost_changed or old != float(new)
            self.change_log.append(SettingChange(name, old, new, now, self.current_period))
        return cost_changed

    def _rekey_all(self) -> None:
        for e in list(self._entries.values()):
            self._detach(e)
            e.priority = self.priority_for(e.key.expected_loss)
            e.sort = (-e.priority, e.key.created_at, e.key.seq_no, e.key.case_id)
            if e.placement is Placement.ACTIVE and e.priority <= 0 and not e.key.canary:
                self._to_backlog(e, "nonpositive")
            elif e.placement is Placement.ACTIVE:
                self.active.add(e.sort)
            else:
                self.backlog.add(e.sort)

    def _detach(self, e: _Entry) -> None:
        if e.placement is Placement.ACTIVE:
            self.active.discard(e.sort)
        elif e.placement is Placement.BACKLOG:
            self.backlog.discard(e.sort)

    def _admit_or_backlog(self, e: _Entry) -> None:
        if e.priority <= 0:
            self._to_backlog(e, "nonpositive")
        elif self.admitted_now < self.alert_budget:
            self._admit(e, "admit")
        else:
            victim = self._displacement_candidate()
            if victim is not None and victim.priority < e.priority:
                self.active.remove(victim.sort)
                self._to_backlog(victim, "displace")  # frees the slot it held
                self._admit(e, "admit")
            else:
                self._to_backlog(e, "backlog")

    def _displacement_candidate(self) -> Optional[_Entry]:
        """Lowest-priority unopened Case admitted this period; later-created among ties."""
        for sort in reversed(self.active):
            e = self._entries[sort[3]]
            if e.admitted_period == self.current_period and not e.key.opened and not e.key.canary:
                return e
        return None

    def _admit(self, e: _Entry, reason: str, *, counted: bool = True) -> None:
        e.admitted_period = self.current_period
        e.counted = counted
        if counted:
            self.admitted_now += 1
        self.active.add(e.sort)
        self._move(e, Placement.ACTIVE, reason)

    def _to_backlog(self, e: _Entry, reason: str) -> None:
        """``e`` must already be detached from the Active_Queue."""
        if e.placement is Placement.ACTIVE and e.admitted_period == self.current_period and e.counted:
            self.admitted_now -= 1
        self.backlog.add(e.sort)
        if e.placement is not Placement.BACKLOG:
            self._move(e, Placement.BACKLOG, reason)

    def _move(self, e: _Entry, new: Placement, reason: str) -> None:
        old = e.placement
        e.placement = new
        self._moves.append(Move(e.key.case_id, old, new, self.current_period, self.clock.now(), reason))
