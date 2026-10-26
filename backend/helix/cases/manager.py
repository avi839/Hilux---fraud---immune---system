"""Case_Manager: Cases, membership indexes and the investigator workflow (design §11).

Routing (Req 13.4-13.6), Motif grouping and merge (Req 12), risk / priority / tier
recompute after every add, merge or create (Req 12.7, 13.9, 13.10) and the
OPEN -> IN_REVIEW -> CLOSED workflow (Req 18).

Change notifications: every mutation appends ``CaseChange`` records to an internal
list (``drain_changes``) and calls each registered listener, so the pipeline can
publish Case updates without the Case_Manager knowing about the API.

Kill chain (Req 29, only with ``kill_chain_multiplier`` set, i.e. ``soc.enabled``): the
pipeline reconstructs each touched Case's kill chain from its Evidence_Chain and hands it to
``set_kill_chain``. ``_recompute`` keeps ``expected_loss_base`` and multiplies Expected_Loss
by the Kill_Chain_Multiplier when the stored most advanced stage is CASH_OUT or LAYERING,
before Case_Priority is derived (Req 29.6, 29.7).

Bounded Cases (task 19.1): on a dense graph, Motif grouping used to chain-merge Cases into a
few huge ones (hundreds of accounts) and every later event paid O(Case size). Growth is now
capped by ``risk.case_max_accounts`` / ``risk.case_max_events``:

- routing (Req 13.4) adds an event to the most recently updated open Case holding its source
  account *that has room* for it; when every such Case is full a new Case is created;
- grouping (Req 12.1-12.5) keeps the earliest Case as survivor. The Motif instance's own
  events and accounts go in first; if they do not fit, its ungrouped events start a new
  (overflow) Case with the instance, or, when it has none, the instance is attached to the
  survivor as evidence only. Other hit Cases are then merged in survivor order while the
  union still fits; a Case that would overflow the survivor stays open, unmerged.

- Motif instances per Case are capped by ``risk.case_max_motifs``: at the cap a Case takes
  only an instance that enters its stage-2 selection (new MotifType or higher FRAUD mass), so
  its risk is unchanged; on a dense graph the detectors re-find overlapping sub-chains of the
  same evidence thousands of times. An instance over evidence the Case fully holds is then a
  no-op (no history, no recompute); ``motifs_dropped`` counts the instances not attached.

A Case created from a single event or Motif instance is never refused, so the bound holds for
every Case whose creating instance fits within it. Each Transaction_Event still belongs to at
most one open Case (Req 12.6). Overflow Cases carry an ``overflow`` history entry naming the
full Case; ``overflows`` counts them.

Recompute cost (Req 12.7, 13.9) is O(1) in the Case size: the total amount, the best stage-1
assessment and the per-MotifType stage-2 selection are maintained incrementally on the Case.
With ``track_deltas`` on (the pipeline), the events and Motif instances attached to each Case
since the last ``drain_delta`` are recorded so the Evidence_Chain can be extended, not rebuilt.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Optional, Union

from helix.cases.models import (
    MAX_NOTE_CHARS,
    SYSTEM,
    Case,
    CaseChange,
    CaseConflict,
    CaseHistoryEntry,
    CaseNotFound,
    CaseStatus,
    CaseValidationError,
    ClosureReason,
    Label,
    Placement,
    Scalar,
    case_id_for,
)
from helix.cases.killchain import KillChain, apply_multiplier, check_multiplier
from helix.cases.prioritiser import CaseKey, Prioritiser, to_decimal
from helix.codec import TransactionEvent
from helix.config import SeverityTier
from helix.config.schema import RiskConfig
from helix.fusion import VACUOUS, Contribution, ContributorType, FusedAssessment
from helix.motifs import MotifInstance, MotifType, contributor_id
from helix.risk import assess_case, best_assessment, risk, tier, tier_rank
from helix.sim.clock import SimClock

_CENT = Decimal("0.01")
_ZERO = Decimal("0")
_HIGH = tier_rank(SeverityTier.HIGH)
DEFAULT_MAX_ACCOUNTS = 50
DEFAULT_MAX_EVENTS = 500
DEFAULT_MAX_MOTIFS = 100

Listener = Callable[[CaseChange], None]


def _rank(fa: FusedAssessment) -> tuple[float, str]:
    """``best_assessment`` order: highest risk first, then the smallest tx_id."""
    return (-risk(fa), fa.tx_id)


def _better_motif(mid: str, m: MotifInstance, cur: Optional[tuple[str, MotifInstance]]) -> bool:
    """``stage2_motifs`` order: highest FRAUD mass per MotifType, ties to the lowest motif id."""
    return cur is None or m.mass.f > cur[1].mass.f or (m.mass.f == cur[1].mass.f and mid < cur[0])


def _vacuous(tx_id: str) -> FusedAssessment:
    """Stand-in stage-1 assessment for a Motif-only Case with no scored event supplied."""
    return FusedAssessment(tx_id, 1, VACUOUS, 0.0, 1.0, 1.0, 0.0, False, ())


def stage2_motifs(motifs: Mapping[str, MotifInstance]) -> list[tuple[str, MotifInstance]]:
    """The Motif instances that contribute to a Case's stage-2 fusion: at most one per
    MotifType, the one with the highest FRAUD mass (ties: lowest motif id), so overlapping
    instances of one type (e.g. sub-chains of a MULE_CHAIN) count their evidence once.
    Returned in motif-id order. All instances stay on ``case.motifs`` for explanation."""
    best: dict[MotifType, tuple[str, MotifInstance]] = {}
    for mid, m in sorted(motifs.items()):
        cur = best.get(m.motif_type)
        if cur is None or m.mass.f > cur[1].mass.f:  # sorted ids: ties keep the lowest id
            best[m.motif_type] = (mid, m)
    return sorted(best.values(), key=lambda p: p[0])


class CaseManager:
    def __init__(
        self,
        risk_cfg: RiskConfig,
        prioritiser: Prioritiser,
        clock: SimClock,
        *,
        high_conflict: float = 0.95,
        motif_weights: Optional[Mapping[MotifType, float]] = None,
        on_high: Optional[Callable[[Case], None]] = None,  # ProfileStore.withhold hook
        kill_chain_multiplier: Optional[float] = None,  # None: kill chain off (soc.enabled False)
        max_accounts: Optional[int] = None,  # default: risk_cfg.case_max_accounts
        max_events: Optional[int] = None,  # default: risk_cfg.case_max_events
        max_motifs: Optional[int] = None,  # default: risk_cfg.case_max_motifs
        track_deltas: bool = False,
    ) -> None:
        self.kill_chain_multiplier: Optional[float] = (
            None if kill_chain_multiplier is None else check_multiplier(kill_chain_multiplier))
        self.max_accounts = int(max_accounts if max_accounts is not None
                                else getattr(risk_cfg, "case_max_accounts", DEFAULT_MAX_ACCOUNTS))
        self.max_events = int(max_events if max_events is not None
                              else getattr(risk_cfg, "case_max_events", DEFAULT_MAX_EVENTS))
        self.max_motifs = int(max_motifs if max_motifs is not None
                              else getattr(risk_cfg, "case_max_motifs", DEFAULT_MAX_MOTIFS))
        if self.max_accounts < 2 or self.max_events < 1 or self.max_motifs < 1:
            raise ValueError(f"case bounds must be >= 2 accounts, >= 1 event and >= 1 motif, got "
                             f"{self.max_accounts} / {self.max_events} / {self.max_motifs}")
        self.motifs_dropped = 0  # instances not attached to a Case already holding max_motifs
        self.track_deltas = track_deltas
        self._delta: dict[str, tuple[set[str], list[str]]] = {}
        self.overflows = 0  # Cases created because the Case that would have grown was full
        self._last_noop: Optional[Case] = None
        self._synced_period: Optional[int] = None
        self.cfg = risk_cfg
        self.prioritiser = prioritiser
        self.clock = clock
        self.high_conflict = high_conflict
        self.motif_weights = dict(motif_weights or {})
        self.on_high = on_high
        self.cases: dict[str, Case] = {}
        self.tx_to_case: dict[str, str] = {}  # open Cases only
        #: Cases holding a Canary reference (Req 33.3, 33.4): Prioritiser exception. Only
        #: filled through ``on_scored_event(force_case=True)`` (soc.enabled with Canaries).
        self.canary_cases: set[str] = set()
        self.account_to_cases: dict[str, set[str]] = {}  # open Cases only
        self.listeners: list[Listener] = []
        self._changes: list[CaseChange] = []
        self._next_seq = 1
        self._change_seq = 0
        self._touch = 0

    # --- queries ---------------------------------------------------------------

    def get(self, case_id: str) -> Case:
        c = self.cases.get(case_id)
        if c is None:
            raise CaseNotFound(case_id, "no such case")
        return c

    def open_cases(self) -> list[Case]:
        return [c for c in self.cases.values() if c.is_open]

    def case_of(self, tx_id: str) -> Optional[str]:
        return self.tx_to_case.get(tx_id)

    def subscribe(self, listener: Listener) -> None:
        self.listeners.append(listener)

    def drain_changes(self) -> list[CaseChange]:
        out, self._changes = self._changes, []
        return out

    def drain_delta(self, case_id: str) -> Optional[tuple[set[str], list[str]]]:
        """Events (attached or refreshed) and Motif ids newly attached to ``case_id`` since
        the last drain, or None if nothing was attached (only with ``track_deltas``)."""
        return self._delta.pop(case_id, None)

    def has_room(self, case: Case, new_accounts: int, new_events: int) -> bool:
        """Whether ``case`` can take this many more accounts and events within the bound."""
        return (len(case.accounts) + new_accounts <= self.max_accounts
                and len(case.tx_ids) + new_events <= self.max_events)

    def stage2_selection(self, case: Case) -> list[tuple[str, MotifInstance]]:
        """``stage2_motifs(case.motifs)`` from the incrementally kept per-type selection."""
        if case.stage2_n != len(case.motifs):
            case.stage2_sel = {m.motif_type: (mid, m) for mid, m in stage2_motifs(case.motifs)}
            case.stage2_n = len(case.motifs)
        return sorted(case.stage2_sel.values(), key=lambda p: p[0])

    # --- ingestion ----------------------------------------------------------------

    def on_scored_event(self, ev: TransactionEvent, fa: FusedAssessment, risk: float, force_case: bool = False) -> Optional[CaseChange]:
        """Route an alerting event (Req 13.4-13.6); below threshold leaves membership unchanged.
        
        When force_case is True (canary touch, Req 33.3), creates a Case even if risk is below threshold."""
        if risk < self.cfg.alert_threshold and not force_case:
            return None
        now = self.clock.now()
        cid = self.tx_to_case.get(ev.tx_id)
        full: Optional[Case] = None
        if cid is not None:  # already grouped (e.g. by a Motif): refresh, never duplicate
            case = self.cases[cid]
            action = "add"
        else:
            hits = [self.cases[c] for c in self.account_to_cases.get(ev.src, ())]
            ends = {ev.src, ev.dst}
            room = [c for c in hits if self.has_room(c, sum(a not in c.accounts for a in ends), 1)]
            if room:
                case = max(room, key=lambda c: (c.updated_at, c.update_no))
                action = "add"
            else:
                if hits:  # every Case holding the source account is full
                    full = max(hits, key=lambda c: (c.updated_at, c.update_no))
                case = self._new_case(now)
                action = "create"
        self._attach_tx(case, ev.tx_id, ev.amount, fa, (ev.src, ev.dst))
        self._record(case, now, action, None, ev.tx_id if action == "add" else CaseStatus.OPEN.value, SYSTEM)
        if full is not None:
            self._overflow(case, full, now)
        if force_case:
            self.canary_cases.add(case.case_id)
        return self._after_change(case, now, action, initial=action == "create")

    def on_motif(
        self,
        inst: MotifInstance,
        assessments: Optional[Mapping[str, FusedAssessment]] = None,
        amounts: Optional[Mapping[str, Decimal]] = None,
    ) -> CaseChange:
        """Group a Motif instance: 0 hits -> create, 1 -> add, 2+ -> merge then add (Req 12.1-12.6).

        ``assessments`` / ``amounts`` supply stage-1 results and amounts for the instance's
        events; amounts default to the instance's edges.
        """
        out = self.on_motifs([(inst, assessments, amounts)])
        if out:
            return out[-1]
        # Dropped by ``max_motifs``: report the unchanged Case that holds its evidence.
        return self._emit(self._last_noop, self.clock.now(), "add")

    def on_motifs(
        self,
        items: Iterable[tuple[MotifInstance, Optional[Mapping[str, FusedAssessment]], Optional[Mapping[str, Decimal]]]],
    ) -> list[CaseChange]:
        """Group several Motif instances (one event's) as ``on_motif`` would, one after the
        other, but recompute each grown Case once at the end rather than once per instance
        (still before the Prioritiser next ranks Cases, Req 12.7). A Case created by an
        instance is recomputed at once, so its creation risk is that of its first instance.
        Returns the changes: one per created Case, then one per grown open Case."""
        now = self.clock.now()
        out: list[CaseChange] = []
        grown: dict[str, list[str]] = {}  # case_id -> merged-from ids, transitively
        self._last_noop = None
        for inst, fas, amts in items:
            case, action, merged_from = self._group(inst, fas, amts, now)
            if action == "noop":  # dropped by max_motifs: the Case is unchanged
                self._last_noop = case
                continue
            mf: list[str] = []
            for loser in merged_from:  # a grown Case merged away hands its losers on
                mf.append(loser)
                mf.extend(grown.pop(loser, ()))
            if action == "create":
                out.append(self._after_change(case, now, action, initial=True))
            else:
                grown.setdefault(case.case_id, []).extend(mf)
        for cid, mf in grown.items():
            case = self.cases[cid]
            if case.is_open:
                out.append(self._after_change(case, now, "merge" if mf else "add", initial=False,
                                              merged_from=tuple(mf)))
        return out

    def _group(
        self,
        inst: MotifInstance,
        assessments: Optional[Mapping[str, FusedAssessment]],
        amounts: Optional[Mapping[str, Decimal]],
        now: datetime,
    ) -> tuple[Case, str, tuple[str, ...]]:
        """Membership part of ``on_motif`` (no recompute): ``(case, action, merged_from)``."""
        owner = self.tx_to_case
        hit_ids = {owner[x] for x in inst.tx_ids if x in owner}
        hits = sorted((self.cases[c] for c in hit_ids), key=lambda c: (c.created_at, c.seq_no))
        inst_accounts = list(dict.fromkeys([*inst.accounts, *(a for e in inst.edges for a in (e.src, e.dst))]))
        ungrouped = [x for x in inst.tx_ids if x not in owner]
        merged_from: tuple[str, ...] = ()
        full: Optional[Case] = None
        evidence_only = False
        if not hits:
            case, action = self._new_case(now), "create"
        else:
            case, action = hits[0], "add"  # survivor: earliest created, then lowest id (Req 12.4)
            extra = {a for a in inst_accounts if a not in case.accounts}
            if not self.has_room(case, len(extra), len(ungrouped)):
                if ungrouped:  # overflow: the ungrouped events start their own Case
                    full, case, action = case, self._new_case(now), "create"
                else:  # nothing left to group: keep the instance as evidence only
                    evidence_only = True
            else:
                n_acc, n_tx = len(case.accounts) + len(extra), len(case.tx_ids) + len(ungrouped)
                losers = []
                for c in hits[1:]:  # merge while the union stays within the bound
                    new = {a for a in c.accounts if a not in case.accounts and a not in extra}
                    if n_acc + len(new) <= self.max_accounts and n_tx + len(c.tx_ids) <= self.max_events:
                        losers.append(c)
                        extra |= new
                        n_acc, n_tx = n_acc + len(new), n_tx + len(c.tx_ids)
                if losers:
                    merged_from = tuple(c.case_id for c in losers)
                    self._merge(case, losers, now)
                    action = "merge"

        if (action == "add" and not ungrouped and len(case.motifs) >= self.max_motifs
                and inst.motif_id not in case.motifs and all(a in case.accounts for a in inst_accounts)
                and not self._takes_motif(case, inst.motif_id, inst)):
            # A Case already holding ``max_motifs`` instances gains nothing from another one
            # over evidence it holds (typically a re-detected sub-chain): no history, no recompute.
            self.motifs_dropped += 1
            return case, "noop", ()
        if not evidence_only:
            edge_amounts = {e.tx_id: e.amount for e in inst.edges}
            edge_accounts = {e.tx_id: (e.src, e.dst) for e in inst.edges}
            assessments = assessments or {}
            amounts = amounts or {}
            for tx in sorted(inst.tx_ids):
                if owner.get(tx, case.case_id) != case.case_id:
                    continue  # held by another open Case that was not merged: it stays there
                amt = edge_amounts.get(tx, amounts.get(tx))
                self._attach_tx(case, tx, amt, assessments.get(tx), edge_accounts.get(tx, ()))
            for a in inst_accounts:
                self._attach_account(case, a)
        self._attach_motif(case, inst.motif_id, inst)  # earlier entries are never replaced
        self._record(case, now, action if action != "merge" else "add",
                     None, inst.motif_id if action != "create" else CaseStatus.OPEN.value, SYSTEM)
        if full is not None:
            self._overflow(case, full, now)
        return case, action, merged_from

    # --- kill chain (Req 29) -----------------------------------------------------------

    def set_kill_chain_multiplier(self, m: float) -> float:
        """Change the Kill_Chain_Multiplier; below 1 (or non-finite) raises ValueError and
        the previous value is kept (Req 29.8). Applies from the next recompute."""
        self.kill_chain_multiplier = check_multiplier(m)
        return self.kill_chain_multiplier

    def set_kill_chain(self, case_id: str, kc: KillChain) -> Optional[CaseChange]:
        """Store a Case's reconstructed kill chain. When its most advanced stage changes, the
        Expected_Loss / Case_Priority are recomputed and a ``kill_chain`` change is emitted."""
        case = self.get(case_id)
        case.kill_chain = kc
        if kc.most_advanced is case.kill_chain_stage or not case.is_open:
            return None
        now = self.clock.now()
        prev = case.kill_chain_stage
        case.kill_chain_stage = kc.most_advanced
        self._record(case, now, "kill_chain", None if prev is None else prev.value,
                     None if kc.most_advanced is None else kc.most_advanced.value, SYSTEM)
        return self._after_change(case, now, "kill_chain", initial=False)

    def tick(self, now: Optional[datetime] = None) -> list[CaseChange]:
        """Advance the Prioritiser's budget period and sync placements/priorities."""
        now = now or self.clock.now()
        before = len(self._changes)
        self.prioritiser.on_tick(now)
        # Priorities only change when a budget period rolls (pending Investigation_Cost), which
        # may also happen inside an upsert: scan the open Cases once per period, not per event.
        if self.prioritiser.current_period != self._synced_period:
            self._synced_period = self.prioritiser.current_period
            for case in self.open_cases():
                p = self.prioritiser.priority_of(case.case_id)
                if p is not None and p != case.priority:
                    case.priority = p
                    self._emit(case, now, "priority")
        self._sync_moves(now)
        return self._changes[before:]

    # --- workflow (Req 18) ---------------------------------------------------------

    def open(self, case_id: str, investigator: str) -> Case:
        case = self.get(case_id)
        if case.status is CaseStatus.CLOSED:
            raise CaseConflict(case_id, "case is CLOSED and cannot be opened", case.assignee)
        if case.status is CaseStatus.IN_REVIEW:
            if case.assignee != investigator:
                raise CaseConflict(case_id, f"case is assigned to {case.assignee}", case.assignee)
            return case  # idempotent re-open by the assignee
        _require_actor(case_id, investigator)
        now = self.clock.now()
        case.status, case.assignee, case.opened_at = CaseStatus.IN_REVIEW, investigator, now
        self._touch_case(case, now)
        self._record(case, now, "open", CaseStatus.OPEN.value, CaseStatus.IN_REVIEW.value, investigator)
        self.prioritiser.mark_opened(case_id)  # opened Cases are never displaced; no re-admission
        self._emit(case, now, "open")
        return case

    def submit_label(self, case_id: str, by: str, label: Union[Label, str], note: str) -> Case:
        case = self.get(case_id)
        try:
            lab = Label(label)
        except ValueError:
            raise CaseValidationError(
                case_id, f"label must be CONFIRMED_FRAUD or FALSE_POSITIVE, got {label!r}"
            ) from None
        if not isinstance(note, str):
            raise CaseValidationError(case_id, "note must be a string")
        if len(note) > MAX_NOTE_CHARS:
            raise CaseValidationError(case_id, f"note is {len(note)} characters; the maximum is {MAX_NOTE_CHARS}")
        if case.status is not CaseStatus.IN_REVIEW:
            raise CaseConflict(case_id, f"case status is {case.status.value}, labels need IN_REVIEW", case.assignee)
        if by != case.assignee:
            raise CaseConflict(case_id, f"only the assignee {case.assignee} may label this case", case.assignee)
        now = self.clock.now()
        case.label, case.label_note, case.label_by, case.label_at = lab, note, by, now
        case.status = CaseStatus.CLOSED
        case.closure_reason, case.closed_at = ClosureReason.LABELLED, now
        self._touch_case(case, now)
        self._record(case, now, "label", CaseStatus.IN_REVIEW.value, CaseStatus.CLOSED.value, by)
        self._unindex(case)
        self.prioritiser.remove(case.case_id)
        self._sync_moves(now, quiet=case.case_id)
        self._emit(case, now, "label")
        return case

    def auto_clear(self, case_id: str, now: Optional[datetime] = None) -> Case:
        """Close an idle OPEN Case for Auto_Triage (Req 41.1): status CLOSED, Closure_Reason
        AUTO_CLEARED, no Feedback_Label, a ``system`` history entry, removed from the
        Active_Queue / Backlog like a labelled closure. No feedback hook runs here or in the
        caller: an AUTO_CLEARED closure is not a Feedback_Label (Req 41.2). Eligibility is
        the caller's (``AutoTriage.eligible``); only an OPEN Case can be cleared."""
        case = self.get(case_id)
        if case.status is not CaseStatus.OPEN:
            raise CaseConflict(case_id, f"case status is {case.status.value}, auto-clear needs OPEN", case.assignee)
        now = now or self.clock.now()
        case.status = CaseStatus.CLOSED
        case.closure_reason, case.closed_at = ClosureReason.AUTO_CLEARED, now
        self._touch_case(case, now)
        self._record(case, now, "auto_clear", CaseStatus.OPEN.value, CaseStatus.CLOSED.value, SYSTEM)
        self._unindex(case)
        self.prioritiser.remove(case.case_id)
        self._sync_moves(now, quiet=case.case_id)
        self._emit(case, now, "auto_clear")
        return case

    def escalate(self, case_id: str, by: str) -> Case:
        case = self.get(case_id)
        if case.status is CaseStatus.CLOSED:
            raise CaseConflict(case_id, "case is CLOSED and cannot be escalated", case.assignee)
        _require_actor(case_id, by)
        now = self.clock.now()
        prev = case.tier
        case.tier, case.escalated, case.escalated_at, case.escalated_by = SeverityTier.CRITICAL, True, now, by
        self._touch_case(case, now)
        self._record(case, now, "escalate", prev.value, SeverityTier.CRITICAL.value, by)
        self._maybe_withhold(case)
        self._emit(case, now, "escalate")
        return case

    # --- internals -------------------------------------------------------------------

    def _new_case(self, now: datetime) -> Case:
        seq = self._next_seq
        self._next_seq += 1
        case = Case(
            case_id=case_id_for(seq), seq_no=seq, status=CaseStatus.OPEN, tier=SeverityTier.LOW,
            risk=0.0, risk_at_creation=0.0, expected_loss=Decimal("0.00"), priority=Decimal("0.00"),
            accounts=set(), tx_ids=set(), assignee=None, created_at=now, updated_at=now,
            admitted_period=None, placement=Placement.NONE, merged_into=None, label=None,
            label_note=None, escalated=False, history=[],
            amount_n=0, best_n=0, stage2_n=0,
        )
        self.cases[case.case_id] = case
        self._touch_case(case, now)
        return case

    def _overflow(self, case: Case, full: Case, now: datetime) -> None:
        """``case`` was created because ``full`` had no room for the new evidence."""
        self.overflows += 1
        self._record(case, now, "overflow", full.case_id, case.case_id, SYSTEM)

    def _note_delta(self, case: Case, tx: Optional[str] = None, motif_id: Optional[str] = None) -> None:
        if not self.track_deltas:
            return
        d = self._delta.get(case.case_id)
        if d is None:
            d = self._delta[case.case_id] = (set(), [])
        if tx is not None:
            d[0].add(tx)
        if motif_id is not None:
            d[1].append(motif_id)

    def _attach_tx(
        self, case: Case, tx: str, amount: Optional[Decimal], fa: Optional[FusedAssessment], accounts: Iterable[str]
    ) -> None:
        case.tx_ids.add(tx)
        self.tx_to_case[tx] = case.case_id
        amts = case.amounts
        old = amts.get(tx)
        new = Decimal(amount) if amount is not None else (old if old is not None else _ZERO)
        amts[tx] = new
        if case.amount_n == len(amts) - (old is None):  # running total stays exact
            case.amount_total += new - (old if old is not None else _ZERO)
            case.amount_n = len(amts)
        else:
            case.amount_n = -1
        if fa is not None:
            prev = case.assessments.get(tx)
            case.assessments[tx] = fa
            self._track_best(case, tx, fa, prev is None)
        self._note_delta(case, tx=tx)
        for a in accounts:
            self._attach_account(case, a)

    def _track_best(self, case: Case, tx: str, fa: FusedAssessment, inserted: bool) -> None:
        """Keep ``case.best_fa`` equal to ``best_assessment(case.assessments.values())``."""
        if case.best_n != len(case.assessments) - inserted:
            case.best_n = -1
            return
        b = case.best_fa
        if b is None:
            if case.best_n != 0:
                case.best_n = -1
                return
            case.best_fa = fa
        elif _rank(fa) < _rank(b):
            case.best_fa = fa
        elif b.tx_id == tx:  # the best event's assessment was replaced by one not better
            if _rank(fa) != _rank(b):
                case.best_n = -1
                return
            case.best_fa = fa
        case.best_n = len(case.assessments)

    def _best(self, case: Case) -> Optional[FusedAssessment]:
        if case.best_n != len(case.assessments):
            case.best_fa = best_assessment(list(case.assessments.values())) if case.assessments else None
            case.best_n = len(case.assessments)
        return case.best_fa

    def _amount_total(self, case: Case) -> Decimal:
        if case.amount_n != len(case.amounts):
            case.amount_total = sum(case.amounts.values(), _ZERO)
            case.amount_n = len(case.amounts)
        return case.amount_total

    def _attach_motif(self, case: Case, mid: str, inst: MotifInstance) -> None:
        """Attach a Motif instance once (earlier entries are never replaced). A Case holding
        ``max_motifs`` instances takes only one that changes its stage-2 selection (a new
        MotifType, or a higher FRAUD mass), so its risk equals that of attaching them all."""
        if mid in case.motifs:
            return
        if not self._takes_motif(case, mid, inst):
            self.motifs_dropped += 1
            return
        case.motifs[mid] = inst
        if case.stage2_n == len(case.motifs) - 1:
            cur = case.stage2_sel.get(inst.motif_type)
            if _better_motif(mid, inst, cur):
                case.stage2_sel[inst.motif_type] = (mid, inst)
            case.stage2_n = len(case.motifs)
        else:
            case.stage2_n = -1
        self._note_delta(case, motif_id=mid)

    def _takes_motif(self, case: Case, mid: str, inst: MotifInstance) -> bool:
        """Below ``max_motifs`` every instance is taken; at the bound only one that would
        enter the stage-2 selection (a new MotifType or a better one of its type)."""
        if len(case.motifs) < self.max_motifs:
            return True
        self.stage2_selection(case)  # brings stage2_sel in step with case.motifs
        return _better_motif(mid, inst, case.stage2_sel.get(inst.motif_type))

    def _attach_account(self, case: Case, account: str) -> None:
        case.accounts.add(account)
        self.account_to_cases.setdefault(account, set()).add(case.case_id)

    def _unindex(self, case: Case) -> None:
        for tx in case.tx_ids:
            if self.tx_to_case.get(tx) == case.case_id:
                del self.tx_to_case[tx]
        for a in case.accounts:
            s = self.account_to_cases.get(a)
            if s is not None:
                s.discard(case.case_id)
                if not s:
                    del self.account_to_cases[a]

    def _merge(self, survivor: Case, losers: list[Case], now: datetime) -> None:
        """Union losers into ``survivor``; losers become CLOSED with ``merged_into`` (Req 12.2, 12.4, 12.5)."""
        for loser in losers:
            self._unindex(loser)
            for tx in sorted(loser.tx_ids):
                self._attach_tx(survivor, tx, loser.amounts.get(tx), loser.assessments.get(tx), ())
            for a in loser.accounts:
                self._attach_account(survivor, a)
            for mid, inst in sorted(loser.motifs.items()):
                self._attach_motif(survivor, mid, inst)
            self._delta.pop(loser.case_id, None)  # its chain is final
            if loser.case_id in self.canary_cases:  # the survivor now holds the Canary reference
                self.canary_cases.add(survivor.case_id)
            if loser.escalated and not survivor.escalated:
                survivor.escalated, survivor.escalated_at, survivor.escalated_by = (
                    True, loser.escalated_at, loser.escalated_by)
            prev_status = loser.status
            loser.status, loser.merged_into = CaseStatus.CLOSED, survivor.case_id
            loser.closure_reason, loser.closed_at = ClosureReason.MERGED, now
            self._touch_case(loser, now)
            self._record(loser, now, "merge", prev_status.value, CaseStatus.CLOSED.value, SYSTEM)
            self._record(survivor, now, "merge", loser.case_id, survivor.case_id, SYSTEM)
            self.prioritiser.remove(loser.case_id)
            self._sync_moves(now, quiet=loser.case_id)
            self._emit(loser, now, "merged")

    def _recompute(self, case: Case, now: datetime, initial: bool) -> None:
        """Risk_Score, Expected_Loss, Case_Priority and Severity_Tier (Req 12.7, 13.2, 13.3, 13.7)."""
        # O(1) in the Case size: the best assessment, the stage-2 selection and the total are
        # kept incrementally (``assess_case`` of the best alone equals that of all of them).
        best = self._best(case)
        fas = [best if best is not None else _vacuous(min(case.tx_ids))]
        contribs = [
            Contribution(contributor_id(m.motif_type), ContributorType.MOTIF, m.mass,
                         float(self.motif_weights.get(m.motif_type, 1.0)))
            for _, m in self.stage2_selection(case)  # one per MotifType: no double counting
        ]
        cr = assess_case(fas, contribs, self.high_conflict)
        case.case_risk = cr
        case.risk = cr.score
        if initial:
            case.risk_at_creation = cr.score
        total = self._amount_total(case)
        case.expected_loss = (to_decimal(cr.score) * total).quantize(_CENT, rounding=ROUND_HALF_EVEN)
        if self.kill_chain_multiplier is not None:  # Req 29.6: multiply before Case_Priority
            case.expected_loss_base = case.expected_loss
            case.expected_loss = apply_multiplier(case.expected_loss, case.kill_chain_stage,
                                                  self.kill_chain_multiplier)
        case.priority = self.prioritiser.priority_for(case.expected_loss)
        new_tier = SeverityTier.CRITICAL if case.escalated else tier(cr.score, self.cfg.bands)
        if new_tier is not case.tier:
            if not initial:
                self._record(case, now, "tier", case.tier.value, new_tier.value, SYSTEM)
            case.tier = new_tier
        self._maybe_withhold(case)

    def _maybe_withhold(self, case: Case) -> None:
        if not case.withheld and tier_rank(case.tier) >= _HIGH:
            case.withheld = True
            if self.on_high is not None:
                self.on_high(case)

    def _after_change(
        self, case: Case, now: datetime, action: str, *, initial: bool, merged_from: tuple[str, ...] = ()
    ) -> CaseChange:
        self._touch_case(case, now)
        self._recompute(case, now, initial)
        self._upsert(case)
        self._sync_moves(now, quiet=case.case_id)  # the primary change below carries the placement
        return self._emit(case, now, action, merged_from=merged_from)

    def _upsert(self, case: Case) -> None:
        self.prioritiser.upsert(
            CaseKey(case.case_id, case.seq_no, case.created_at, case.expected_loss,
                    opened=case.status is CaseStatus.IN_REVIEW, canary=case.case_id in self.canary_cases)
        )
        p = self.prioritiser.priority_of(case.case_id)
        if p is not None:
            case.priority = p

    def _sync_moves(self, now: datetime, quiet: Optional[str] = None) -> None:
        """Apply Prioritiser moves to Cases; records history and notifies (except for ``quiet``)."""
        for mv in self.prioritiser.drain_moves():
            case = self.cases.get(mv.case_id)
            if case is None or case.placement is mv.new:
                continue
            prev = case.placement
            case.placement = mv.new
            if mv.new is Placement.ACTIVE:
                case.admitted_period = mv.period
            self._record(case, mv.ts, "placement", prev.value, mv.new.value, SYSTEM)
            if mv.case_id != quiet:
                self._emit(case, mv.ts, "placement")

    def _touch_case(self, case: Case, now: datetime) -> None:
        self._touch += 1
        case.updated_at = now
        case.update_no = self._touch

    def _record(self, case: Case, ts: datetime, action: str, prev: Scalar, new: Scalar, actor: str) -> None:
        if case.history and ts < case.history[-1].ts:
            ts = case.history[-1].ts  # history is ascending even if a caller's clock is not
        case.history.append(CaseHistoryEntry(case.case_id, ts, action, prev, new, actor))

    def _emit(self, case: Case, ts: datetime, action: str, merged_from: tuple[str, ...] = ()) -> CaseChange:
        self._change_seq += 1
        ch = CaseChange(
            seq=self._change_seq, ts=ts, case_id=case.case_id, action=action, status=case.status,
            tier=case.tier, risk=case.risk, priority=case.priority, placement=case.placement,
            merged_into=case.merged_into, merged_from=merged_from,
        )
        self._changes.append(ch)
        for fn in list(self.listeners):
            fn(ch)
        return ch


def _require_actor(case_id: str, actor: object) -> None:
    if not isinstance(actor, str) or not actor.strip():
        raise CaseValidationError(case_id, "investigator id must be a non-empty string")
