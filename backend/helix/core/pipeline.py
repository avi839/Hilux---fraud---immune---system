"""HELIX composition root: the synchronous SENSE→ACT pipeline (design Overview, Architecture).

``Helix`` builds every detection component from a ``HelixConfig`` and a ``SimClock`` and
runs one admitted Transaction_Event through all stages with no awaits inside:

1. PERCEIVE  - sensors on the pre-event Behavioural_DNA / CPD state, stage-1 fusion
               (``DefaultStage1Engine.assess``, the exact code replay uses).
2. REMEMBER  - Profile_Store apply (withheld up front when ``src`` has an open HIGH/CRITICAL
               Case and the stage-1 risk is at or above the alerting threshold, design §7),
               then the CPD step (``DefaultStage1Engine.commit``).
3. REASON    - Temporal_Graph add, Motif_Detectors, stage-2 fusion of the event's stage-1
               assessment with its Motif contributions.
4. ACT       - Risk_Score from stage 1 (Req 13.1), Case routing and Motif grouping,
               Prioritiser tick, Evidence_Chain update, Storage, pub/sub.

Per-stage latency is measured with ``perf_counter`` and kept in a ``LatencyRecorder``
(60 s rolling window plus since-start, Req 25.4-25.6).

ACT cost (task 19.1, Req 25): Cases are bounded (``risk.case_max_accounts`` /
``case_max_events``, see ``helix.cases.manager``), and the per-event work on a touched Case
does not grow with its size: risk recompute is incremental, Evidence_Chains are extended with
the new evidence only (``ChainStore``), the kill chain is re-derived only when a new entry
advances it, and Case rows / chains are written to Storage at most once per
``case_persist_interval_s`` per Case (immediately when first seen, CLOSED, or changed by an
investigator; ``flush_cases`` and ``snapshot_state`` write anything pending).

Determinism: given the same config, start clock, event order and a deterministic
``sensor_clock`` (and a motif budget that is not hit), two pipelines produce identical
outcomes. Wall-clock budgets (sensor timeouts, motif truncation) are the only source of
non-determinism, as in the design.

Cyber fusion (Req 28, 29; only when ``cfg.soc.enabled``, otherwise nothing below runs):

- an admitted Auth_Event is not scored: ``process`` records it into ``self.auth`` (the
  per-account ``AuthHistory``), advances stream time and returns an ``AuthOutcome``;
- the Auth_Sensor joins the sensors and reads that history; after PERCEIVE each
  Transaction_Event is linked to its correlated Auth_Events (same account and session or
  device, plus Suspicious_Auth_Activity) and the links become AUTH_EVENT chain entries;
- after the Evidence_Chains of touched Cases are updated, each open Case's kill chain is
  reconstructed from its chain; a change of the most advanced stage re-prioritises the Case
  (Kill_Chain_Multiplier) and its chain is refreshed once more in the same ACT;
- with a Rule_Engine attached (``helix.soc.rules.attach_rules``), the masses of matching active
  Detection_Rules are fused into the stage-1 assessment right after the sensors (RULE
  contributors, recorded in the Evidence_Chain with rule id and version).

Replay (``DefaultStage1Engine`` snapshots) does not restore auth history, so replayed
stage-1 assessments carry a vacuous Auth_Sensor result (honest limitation).
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import math
import random
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Optional, Protocol, Union, runtime_checkable

from helix.cases import Case, CaseChange, CaseManager, CaseStatus, Label, Prioritiser
from helix.cases.suggest import suggest_case
from helix.cases.killchain import KillChain, assign_stage, max_stage, reconstruct, stage_rank
from helix.changepoint import ChangePointRecord
from helix.codec import AuthEvent, TransactionEvent
from helix.config import HelixConfig, SeverityTier, ensure_valid
from helix.explain import (
    AUTH_PREFIX,
    TRUNCATION_PREFIX,
    ChainBuilder,
    EntryKind,
    EvidenceChain,
    EvidenceEntry,
    dumps,
    motif_entries,
    stage2_entry,
    stage2_weights,
    tx_entries,
)
from helix.fusion import FusedAssessment, FusionError, fuse, fuse_stage2
from helix.graph import TemporalGraph
from helix.ingest import IN_ORDER, AdmittedEvent, Lateness
from helix.motifs import EngineResult, MotifEngine, MotifInstance, MotifType
from helix.replay.engine import DefaultStage1Engine
from helix.risk import risk, tier, tier_rank
from helix.sensors import AuthHistory, AuthLink, AuthSensor, SensorAgent, SensorResult, default_sensors
from helix.sensors.base import SensorOutput
from helix.sensors.runner import SensorOutcome
from helix.sim.clock import ManualClock, SimClock

log = logging.getLogger(__name__)

EVENTS = "events"
CASES = "cases"
METRICS = "metrics"
CHANNELS = (EVENTS, CASES, METRICS)

UNAVAILABLE = "unavailable"
LATE_ID = "~late"
CHANGE_POINT_ID = "~cp"

_HIGH = tier_rank(SeverityTier.HIGH)
_CENT = Decimal("0.01")
_EMPTY_RESULT = EngineResult(())


# --- latency -------------------------------------------------------------------------


class Stage(str, Enum):
    PERCEIVE = "PERCEIVE"
    REMEMBER = "REMEMBER"
    REASON = "REASON"
    ACT = "ACT"
    END_TO_END = "END_TO_END"  # PERCEIVE start to ACT end (queue wait not included)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class LatencySummary:
    """Percentiles in ms; all None (reported as "unavailable") when ``count == 0``."""

    count: int
    p50: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None

    @property
    def available(self) -> bool:
        return self.count > 0

    def to_dict(self) -> dict[str, Union[int, float, str]]:
        def v(x: Optional[float]) -> Union[float, str]:
            return x if self.available and x is not None else UNAVAILABLE

        return {"count": self.count, "p50": v(self.p50), "p95": v(self.p95), "p99": v(self.p99),
                "min": v(self.min), "max": v(self.max)}


def percentile(sorted_samples: Sequence[float], q: float) -> float:
    """Nearest-rank percentile: always one of the samples, monotone in ``q``."""
    if not sorted_samples:
        raise ValueError("percentile of no samples")
    n = len(sorted_samples)
    return sorted_samples[min(n - 1, max(0, math.ceil(q * n) - 1))]


def summarise(samples: Iterable[float], count: Optional[int] = None,
              lo: Optional[float] = None, hi: Optional[float] = None) -> LatencySummary:
    s = sorted(samples)
    n = len(s) if count is None else count
    if n == 0 or not s:
        return LatencySummary(0)
    return LatencySummary(n, percentile(s, 0.50), percentile(s, 0.95), percentile(s, 0.99),
                          s[0] if lo is None else lo, s[-1] if hi is None else hi)


class _AllTime:
    """Exact count/min/max plus a seeded reservoir sample for since-start percentiles."""

    __slots__ = ("count", "lo", "hi", "res", "cap", "rng")

    def __init__(self, cap: int, seed: int) -> None:
        self.count, self.lo, self.hi = 0, math.inf, -math.inf
        self.res: list[float] = []
        self.cap = cap
        self.rng = random.Random(seed)

    def add(self, x: float) -> None:
        self.count += 1
        self.lo, self.hi = min(self.lo, x), max(self.hi, x)
        if len(self.res) < self.cap:
            self.res.append(x)
        else:
            j = self.rng.randrange(self.count)
            if j < self.cap:
                self.res[j] = x

    def summary(self) -> LatencySummary:
        if self.count == 0:
            return LatencySummary(0)
        return summarise(self.res, self.count, self.lo, self.hi)


class LatencyRecorder:
    """Per-stage latency: rolling window (default 60 s) and since start (Req 25.4-25.6).

    ``now`` is a monotonic seconds clock (wall time, not stream time). Since-start
    percentiles come from a bounded reservoir sample, so they are always real samples
    and lie within the exact all-time [min, max].
    """

    def __init__(self, window_s: float = 60.0, *, now: Callable[[], float] = time.monotonic,
                 reservoir: int = 10_000, seed: int = 0) -> None:
        if not (math.isfinite(window_s) and window_s > 0):
            raise ValueError(f"window_s must be positive, got {window_s!r}")
        self.window_s = float(window_s)
        self.now = now
        self._win: dict[Stage, deque[tuple[float, float]]] = {s: deque() for s in Stage}
        self._all: dict[Stage, _AllTime] = {s: _AllTime(reservoir, seed + i) for i, s in enumerate(Stage)}

    def record(self, stage: Union[Stage, str], ms: float, at: Optional[float] = None) -> None:
        stage = Stage(stage)
        if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not math.isfinite(ms) or ms < 0:
            raise ValueError(f"latency must be a finite number >= 0 ms, got {ms!r}")
        t = self.now() if at is None else at
        self._win[stage].append((t, float(ms)))
        self._all[stage].add(float(ms))

    def _evict(self, stage: Stage, now: float) -> deque[tuple[float, float]]:
        d = self._win[stage]
        cutoff = now - self.window_s
        while d and d[0][0] < cutoff:
            d.popleft()
        return d

    def window(self, stage: Union[Stage, str], now: Optional[float] = None) -> LatencySummary:
        d = self._evict(Stage(stage), self.now() if now is None else now)
        return summarise(ms for _, ms in d)

    def all_time(self, stage: Union[Stage, str]) -> LatencySummary:
        return self._all[Stage(stage)].summary()

    def report(self, now: Optional[float] = None) -> dict[str, dict[str, dict[str, Union[int, float, str]]]]:
        t = self.now() if now is None else now
        return {
            "window": {s.value: self.window(s, t).to_dict() for s in Stage},
            "since_start": {s.value: self.all_time(s).to_dict() for s in Stage},
        }


# --- pub/sub ---------------------------------------------------------------------------


@runtime_checkable
class Publisher(Protocol):
    def publish(self, channel: str, msg: Mapping[str, Any]) -> None: ...


class NullPublisher:
    def publish(self, channel: str, msg: Mapping[str, Any]) -> None:
        return None


class InProcessPublisher:
    """Synchronous fan-out to subscribers; a failing subscriber never stops the pipeline
    (Req 1.13: processing continues with or without clients)."""

    def __init__(self, history: int = 0) -> None:
        self._subs: dict[str, list[Callable[[Mapping[str, Any]], None]]] = {}
        self.history: dict[str, deque[Mapping[str, Any]]] = {c: deque(maxlen=history) for c in CHANNELS}
        self.errors = 0

    def subscribe(self, channel: str, fn: Callable[[Mapping[str, Any]], None]) -> Callable[[], None]:
        self._subs.setdefault(channel, []).append(fn)

        def unsubscribe() -> None:
            subs = self._subs.get(channel, [])
            if fn in subs:
                subs.remove(fn)

        return unsubscribe

    def publish(self, channel: str, msg: Mapping[str, Any]) -> None:
        h = self.history.get(channel)
        if h is not None and h.maxlen:
            h.append(msg)
        for fn in list(self._subs.get(channel, ())):
            try:
                fn(msg)
            except Exception:  # noqa: BLE001 - a subscriber must not break the worker
                self.errors += 1
                log.exception("subscriber on %s failed", channel)


# --- outcomes ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventOutcome:
    seq_no: int  # pipeline sequence number (1-based, processing order)
    event: TransactionEvent
    lateness: Lateness
    stage1: FusedAssessment
    stage2: FusedAssessment  # stage 1 + this event's Motif contributions
    risk: float  # Risk_Score from the stage-1 assessment (Req 13.1)
    tier: Optional[SeverityTier]  # None below the alerting threshold
    sensor_results: tuple[SensorResult, ...]
    fusion_errors: tuple[FusionError, ...]
    applied: bool  # applied to the Behavioural_DNA (False when withheld up front)
    withheld_up_front: bool
    graph_added: bool
    motifs: tuple[MotifInstance, ...]
    truncated: frozenset[MotifType]
    case_id: Optional[str]  # open Case holding the event after ACT
    case_changes: tuple[CaseChange, ...]
    latency_ms: Mapping[Stage, float]


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    """Result of processing one Auth_Event (Req 28.1)."""

    event: AuthEvent
    recorded: bool  # False: cyber fusion disabled, or the event id is already in the history
    new_device: bool  # LOGIN_SUCCESS from a device not previously seen for the account


@dataclass(slots=True)
class _TxInfo:
    ev: TransactionEvent
    stage1: FusedAssessment
    stage2: FusedAssessment
    results: tuple[SensorResult, ...]
    lateness: Lateness
    received_at: datetime
    truncated: frozenset[MotifType]
    cp: Optional[ChangePointRecord]
    auth: tuple[AuthLink, ...] = ()  # correlated Auth_Events (Req 28.7, 28.8)


FeedbackHook = Callable[[Case, Label], None]


class ChainStore(MutableMapping[str, EvidenceChain]):
    """``case_id -> EvidenceChain`` backed by one ``ChainBuilder`` per Case.

    Reads return the immutable, canonically ordered chain (materialised on demand and cached
    until the Case's next change); the pipeline extends chains through ``builder`` in
    O(new entries). Assigning a chain (e.g. restored from Storage) replaces the Case's builder.
    """

    def __init__(self) -> None:
        self._b: dict[str, ChainBuilder] = {}

    def builder(self, case_id: str) -> Optional[ChainBuilder]:
        return self._b.get(case_id)

    def new_builder(self, case_id: str) -> ChainBuilder:
        b = self._b[case_id] = ChainBuilder(case_id)
        return b

    def __getitem__(self, case_id: str) -> EvidenceChain:
        return self._b[case_id].chain()

    def __setitem__(self, case_id: str, chain: EvidenceChain) -> None:
        if not isinstance(chain, EvidenceChain):
            raise TypeError(f"not an EvidenceChain: {type(chain).__name__}")
        self._b[case_id] = ChainBuilder(case_id, chain)

    def __delitem__(self, case_id: str) -> None:
        del self._b[case_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._b)

    def __len__(self) -> int:
        return len(self._b)

    def __contains__(self, case_id: object) -> bool:
        return case_id in self._b


# --- the composition root ------------------------------------------------------------------


class Helix:
    # Warm_Up_Mode (Req 40.7, design §25): while set, SENSE..REASON still run (DNA, CPD state,
    # Temporal_Graph) but the Case_Manager create/add path and the Self_Set offer are skipped,
    # so no Cases, Prioritiser admissions, Playbook_Actions or labels arise. Set by the
    # Dataset_Feeder only.
    _warmup: bool = False

    @property
    def warmup(self) -> bool:
        return self._warmup

    @warmup.setter
    def warmup(self, on: bool) -> None:
        self._warmup = bool(on)
        try:
            self.stage1.suspend_self_set = bool(on)  # read by ImmuneStage1Engine.commit
        except AttributeError:  # pragma: no cover - a slotted custom engine
            pass

    def __init__(
        self,
        cfg: Optional[HelixConfig] = None,
        clock: Optional[SimClock] = None,
        *,
        storage: Any = None,  # helix.storage.Storage (opened), or None
        publisher: Optional[Publisher] = None,
        weights: Optional[Mapping[str, float]] = None,
        sensors: Optional[Sequence[SensorAgent]] = None,
        motif_engine: Optional[MotifEngine] = None,
        sensor_clock: Callable[[], float] = time.perf_counter,
        perf: Callable[[], float] = time.perf_counter,
        latency: Optional[LatencyRecorder] = None,
        metrics_interval_s: float = 1.0,
        case_persist_interval_s: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = ensure_valid(cfg if cfg is not None else HelixConfig())
        self.clock: SimClock = clock if clock is not None else ManualClock()
        self.weights: dict[str, float] = {}
        self.set_weights(weights or {})

        # Cyber fusion (Req 28, 29): auth history + Auth_Sensor, only when enabled.
        self.soc_enabled = bool(self.cfg.soc.enabled)
        self.auth: Optional[AuthHistory] = AuthHistory(self.cfg.soc) if self.soc_enabled else None
        self.auth_processed = 0
        self.auth_ignored = 0  # Auth_Events seen while cyber fusion is disabled
        #: Rule_Engine (``helix.soc.rules.attach_rules``, needs soc.enabled); None = no rules.
        self.rules: Any = None
        #: CanaryRegistry (``helix.soc.canaries``, needs soc.enabled); None = no canaries.
        self.canaries: Any = None
        #: IndicatorStore (``helix.soc.intel``, needs soc.enabled); None = no indicators.
        self.indicators: Any = None
        #: PlaybookRunner (``helix.soc.playbooks``, needs soc.enabled); None = no playbooks.
        self.playbooks: Any = None
        #: AuditLedger (``helix.soc.ledger``, needs soc.enabled); None = no audit.
        self.audit: Any = None
        if self.auth is not None:
            if sensors is None:
                sensors = default_sensors(self.cfg, auth_history=self.auth)
            elif not any(getattr(s, "name", None) == AuthSensor.name for s in sensors):
                sensors = [*sensors, AuthSensor(self.auth, self.cfg.soc)]

        # PERCEIVE + REMEMBER: the same engine replay uses; it fuses with ``self.weights``.
        self.stage1 = DefaultStage1Engine(self.cfg, sensors=sensors, clock=sensor_clock)
        self.stage1.weights = self.weights  # shared dict: weight changes apply to the next event
        self.profiles = self.stage1.profiles
        self.cpd = self.stage1.cpd

        # REASON
        self.graph = TemporalGraph.from_config(self.cfg.graph)
        self.motifs = motif_engine if motif_engine is not None else MotifEngine.from_config(self.cfg.graph)

        # ACT
        self.prioritiser = Prioritiser(self.cfg.prioritiser, self.clock)
        self.cases = CaseManager(
            self.cfg.risk, self.prioritiser, self.clock,
            high_conflict=self.cfg.fusion.high_conflict,
            motif_weights=self.motifs.weights,
            on_high=self._on_high,
            kill_chain_multiplier=self.cfg.soc.kill_chain_multiplier if self.soc_enabled else None,
            track_deltas=True,  # Evidence_Chains are extended per change, not rebuilt
        )
        #: case_id -> Evidence_Chain (a ``ChainStore``: incremental, materialised on read).
        self.chains = ChainStore()
        self._kc_new: dict[str, list[EvidenceEntry]] = {}  # entries not yet seen by the kill chain
        self._kc_basis: Optional[tuple[Any, Any]] = None

        self.storage = storage
        self.publisher: Publisher = publisher if publisher is not None else NullPublisher()
        self.latency = latency if latency is not None else LatencyRecorder()
        self.metrics_interval_s = float(metrics_interval_s)
        #: Feedback_Learner hooks (task 14.1), called after a label is applied to the DNA.
        self.feedback_hooks: list[FeedbackHook] = []

        self.seq_no = 0
        self.withheld_up_front = 0
        self.storage_errors = 0
        self._perf = perf
        self._recent: dict[str, _TxInfo] = {}  # tx_id -> info, bounded to the graph window
        self._recent_heap: list[tuple[datetime, str]] = []
        self._pinned: dict[str, _TxInfo] = {}  # Case members (kept for Evidence_Chains / labels)
        self._pub_seq = {c: 0 for c in CHANNELS}
        self._history_saved: dict[str, int] = {}
        # Coalesced Case persistence (task 19.1): a Case row + Evidence_Chain is written at once
        # when first seen, when CLOSED or on investigator actions, otherwise at most once per
        # ``case_persist_interval_s`` (wall clock); ``flush_cases`` writes everything pending.
        if not (math.isfinite(case_persist_interval_s) and case_persist_interval_s >= 0):
            raise ValueError(f"case_persist_interval_s must be >= 0, got {case_persist_interval_s!r}")
        self.case_persist_interval_s = float(case_persist_interval_s)
        self._mono = monotonic
        self._persisted_at: dict[str, float] = {}
        self._persist_pending: set[str] = set()
        self._persist_queue: deque[tuple[float, str]] = deque()
        self._last_metrics: Optional[float] = None
        self._stopping = False
        self._ingestor: Any = None

    # --- configuration --------------------------------------------------------------

    def set_weights(self, weights: Mapping[str, float]) -> None:
        """Replace the Sensor_Weights used by stage-1 fusion (in place, shared with the engine)."""
        for k, w in weights.items():
            if isinstance(w, bool) or not isinstance(w, (int, float)) or not (math.isfinite(w) and 0 <= w <= 1):
                raise ValueError(f"weight for {k!r} must be in [0, 1], got {w!r}")
        
        # SOC: Audit ledger hook for weight changes (Req 35.1)
        # getattr: __init__ calls set_weights before the SOC attributes exist.
        if getattr(self, "audit", None) is not None and getattr(self, "soc_enabled", False) and weights:
            try:
                # Record what changed
                changes = {}
                for k, w in weights.items():
                    old = self.weights.get(k)
                    if old != w:
                        changes[k] = {"old": old, "new": w}
                if changes:
                    self.audit.append("SENSOR_WEIGHT_CHANGE", "system", {
                        "changes": changes,
                        "ts": self.clock.now()
                    })
            except Exception:  # noqa: BLE001 - audit faults must not break weight updates
                log.exception("audit ledger failed on weight change")
        
        self.weights.clear()
        self.weights.update({str(k): float(w) for k, w in weights.items()})

    # --- per-event pipeline -------------------------------------------------------------

    def process_auth(self, item: Union[AdmittedEvent, AuthEvent]) -> AuthOutcome:
        """Record one Auth_Event into the account's auth history (Req 28.1). Ignored (not
        recorded, stream time unchanged) when cyber fusion is disabled."""
        ev = item.event if isinstance(item, AdmittedEvent) else item
        if not isinstance(ev, AuthEvent):
            raise TypeError(f"not an AuthEvent: {type(ev).__name__}")
        if self.auth is None:
            self.auth_ignored += 1
            return AuthOutcome(ev, False, False)
        self._advance_clock(ev.ts)
        rec = self.auth.record(ev)
        self.auth_processed += 1
        if self.canaries is not None:  # Canary references in Auth_Events are hits too (Req 33.2)
            self.canaries.observe(ev)
        return AuthOutcome(ev, rec is not None, bool(rec is not None and rec.new_device))

    def process(self, item: Union[AdmittedEvent, TransactionEvent, AuthEvent]) -> Any:
        """Run one admitted event: a Transaction_Event through SENSE→ACT (``EventOutcome``)
        or an Auth_Event into the auth history (``AuthOutcome``)."""
        ev0 = item.event if isinstance(item, AdmittedEvent) else item
        if isinstance(ev0, AuthEvent):
            return self.process_auth(item)  # type: ignore[arg-type]
        return self._process_tx(item)  # type: ignore[arg-type]

    def _process_tx(self, item: Union[AdmittedEvent, TransactionEvent]) -> EventOutcome:
        adm = item if isinstance(item, AdmittedEvent) else AdmittedEvent(item, IN_ORDER, -1, self.clock.now())
        ev = adm.event
        perf = self._perf
        hc = self.cfg.fusion.high_conflict
        thr = self.cfg.risk.alert_threshold

        t0 = perf()
        self._advance_clock(ev.ts)
        self.seq_no += 1
        seq = self.seq_no

        # PERCEIVE: sensors + stage-1 fusion on pre-event state.
        stage1, results, errors = self.stage1.assess(ev)
        if self.rules is not None and self.soc_enabled:  # active Detection_Rules join stage 1 (Req 31.6)
            stage1, results = self.rules.extend_stage1(ev, stage1, results, profiles=self.profiles,
                                                       auth=self.auth, high_conflict=hc)
        
        # SOC: Canary and Indicator contributions join stage-1 fusion (Req 33.2, 34.3, 34.4)
        if self.soc_enabled and (self.canaries is not None or self.indicators is not None):
            stage1, results = self._extend_stage1_soc(ev, stage1, results, hc)

        r = risk(stage1)
        auth_links = self.auth.links(ev) if self.auth is not None else ()  # Req 28.7
        t1 = perf()

        # REMEMBER: profile apply (or withhold up front) + CPD step.
        withhold = not self._warmup and r >= thr and self._has_open_high(ev.src)
        applied = self.stage1.commit(ev, withhold=withhold)
        if withhold:
            self.withheld_up_front += 1
        cst = self.cpd.state(ev.src)
        cp = cst.candidate if cst.candidate is not None and cst.candidate.tx_id == ev.tx_id else None
        t2 = perf()

        # REASON: graph + motifs + stage-2 fusion of this event.
        added = self.graph.add(ev)
        res = self.motifs.on_event(ev, self.graph) if added else _EMPTY_RESULT
        stage2, s2_errors = fuse_stage2(stage1, res.contributions.get(ev.tx_id, ()), hc)
        info = _TxInfo(ev, stage1, stage2, tuple(results), adm.lateness, adm.received_at, res.truncated, cp,
                       auth_links)
        self._recent[ev.tx_id] = info
        heapq.heappush(self._recent_heap, (ev.ts, ev.tx_id))
        t3 = perf()

        # ACT: routing, grouping, prioritisation, evidence, persistence, publishing.
        # A Canary reference forces a Case regardless of the threshold (Req 33.3, 33.4).
        if self._warmup:
            pass  # Warm_Up_Mode: Case creation suppressed (Req 40.7)
        elif self._is_canary_touch(ev):
            self.cases.on_scored_event(ev, stage1, r, force_case=True)
        else:
            self.cases.on_scored_event(ev, stage1, r)
        if res.instances and not self._warmup:  # one recompute per grown Case, not per instance
            self.cases.on_motifs(self._motif_items(res.instances))
        self.cases.tick(self.clock.now())
        changes = self.cases.drain_changes()
        touched = self._update_chains(changes)
        changes, touched = self._kill_chain_pass(changes, touched)
        
        if self.playbooks is not None and self.soc_enabled:  # Req 32.2: simulated state only
            self._run_playbooks(touched)
        case_id = self.cases.case_of(ev.tx_id)
        ev_tier = tier(r, self.cfg.risk.bands) if r >= thr else None
        if self.storage is not None:
            self._persist_event(adm, seq, stage1, stage2, r, res.instances)
            self._persist_cases(touched)
            self._persist_due()
        self._publish_event(info, r, case_id)
        self._publish_cases(changes)
        self._evict_recent()
        t4 = perf()

        lat = {
            Stage.PERCEIVE: (t1 - t0) * 1e3,
            Stage.REMEMBER: (t2 - t1) * 1e3,
            Stage.REASON: (t3 - t2) * 1e3,
            Stage.ACT: (t4 - t3) * 1e3,
            Stage.END_TO_END: (t4 - t0) * 1e3,
        }
        for s, ms in lat.items():
            self.latency.record(s, max(0.0, ms))
        self._maybe_publish_metrics()

        return EventOutcome(
            seq_no=seq, event=ev, lateness=adm.lateness, stage1=stage1, stage2=stage2, risk=r,
            tier=ev_tier, sensor_results=tuple(results), fusion_errors=tuple(errors) + tuple(s2_errors),
            applied=applied, withheld_up_front=withhold, graph_added=added, motifs=res.instances,
            truncated=res.truncated, case_id=case_id, case_changes=tuple(changes),
            latency_ms=MappingProxyType(lat),
        )

    def process_all(self, items: Iterable[Union[AdmittedEvent, TransactionEvent]]) -> list[EventOutcome]:
        """Synchronous driver for tests and batch replays."""
        return [self.process(it) for it in items]

    async def run(self, ingestor: Any, *, max_events: Optional[int] = None) -> int:
        """Pipeline worker: drain ``ingestor`` (``await next_event()``) until ``stop()`` or
        ``max_events``. Cancel the task to stop while it waits for an event."""
        self._stopping = False
        self._ingestor = ingestor
        n = 0
        while not self._stopping:
            adm = await ingestor.next_event()
            self.process(adm)
            n += 1
            if max_events is not None and n >= max_events:
                break
            await asyncio.sleep(0)  # let ingest handlers and the WS hub run
        return n

    def stop(self) -> None:
        self._stopping = True

    # --- investigator actions -----------------------------------------------------------

    def open_case(self, case_id: str, investigator: str) -> Case:
        case = self.cases.open(case_id, investigator)
        self._flush_case_changes()
        return case

    def escalate(self, case_id: str, by: str) -> Case:
        case = self.cases.escalate(case_id, by)
        if self.audit is not None and self.soc_enabled:  # Req 18.9, 35.1 (never raises)
            from helix.soc.ledger import audit_escalation

            audit_escalation(self.audit, case, by, at=self.clock.now())
        self._flush_case_changes()
        return case

    def label(self, case_id: str, by: str, label: Union[Label, str], note: str = "") -> Case:
        """Close a Case with a label: FALSE_POSITIVE releases withheld events into the DNA,
        CONFIRMED_FRAUD excludes them permanently; then the Feedback_Learner hooks run."""
        case = self.cases.submit_label(case_id, by, label, note)
        lab = case.label
        # SOC (Req 18.9, 34.2, 35.1): the Audit_Ledger FEEDBACK_LABEL entry and Indicator
        # extraction both run as ``feedback_hooks`` (``attach_audit`` / ``attach_indicators``),
        # so each label is recorded exactly once; nothing SOC-specific happens here.
        for src, txs in sorted(self._by_src(case).items()):
            if lab is Label.FALSE_POSITIVE:
                self.profiles.release(src, txs)
            else:
                self.profiles.exclude(src, txs)
        for hook in list(self.feedback_hooks):  # Feedback_Learner (task 14.1), SOC audit / intel
            hook(case, lab)  # type: ignore[arg-type]
        self._flush_case_changes()
        return case

    def label_bulk(self, by: str, label: Union[Label, str], note: str, case_ids: Sequence[str]) -> list[Case]:
        """Label several Cases with one Feedback_Label, all or nothing (Req 41.6, 41.7).

        Phase 1 (``validate_bulk``) checks every Case without changing anything and raises
        ``BulkFeedbackError`` listing each failing id. Phase 2 opens each OPEN unassigned Case
        for ``by`` and calls ``label`` in request order, so every Case is a normal
        Feedback_Label: the learner, Immune_System, audit and indicator hooks run once per
        Case. There is no ``await`` between the phases, so no event can interleave."""
        from helix.cases.bulk import validate_bulk

        err = validate_bulk(self.cases, by, label, note, case_ids)
        if err is not None:
            raise err
        out: list[Case] = []
        for cid in case_ids:
            if self.cases.get(cid).status is CaseStatus.OPEN:
                self.open_case(cid, by)
            out.append(self.label(cid, by, label, note))
        return out

    # --- state --------------------------------------------------------------------------

    def snapshot_state(self, last_seq_no: Optional[int] = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(dna, cpd)`` for ``Storage.snapshot``; defaults to the last processed seq_no.
        Also queues every coalesced Case write (``flush_cases``), so periodic and final
        snapshots never leave a Case row or Evidence_Chain behind."""
        self.flush_cases()
        return self.stage1.snapshot_state(self.seq_no if last_seq_no is None else last_seq_no)

    def flush_cases(self) -> int:
        """Queue the pending coalesced Case rows / Evidence_Chains to Storage now; returns how
        many Cases were written. A no-op without Storage."""
        if self.storage is None or not self._persist_pending:
            return 0
        pending = [cid for _, cid in self._persist_queue if cid in self._persist_pending]
        for cid in pending:
            self._write_case(cid)
        self._persist_queue.clear()
        return len(pending)

    def chain(self, case_id: str) -> Optional[EvidenceChain]:
        return self.chains.get(case_id)

    def metrics(self) -> dict[str, Any]:
        q = self._ingestor
        return {
            "processed": self.seq_no,
            "withheld_up_front": self.withheld_up_front,
            "storage_errors": self.storage_errors,
            "queue_length": None if q is None else len(q),
            "latency_ms": self.latency.report(),
        }

    # --- internals ----------------------------------------------------------------------

    def _advance_clock(self, ts: datetime) -> None:
        """Event-driven stream time: clock = max(clock, ev.ts)."""
        now = self.clock.now()
        if ts > now:
            self.clock.advance(ts - now)

    def _motif_items(self, instances: Sequence[MotifInstance]) -> Iterator[tuple[MotifInstance, dict, dict]]:
        for inst in instances:
            infos = {tx: i for tx in inst.tx_ids if (i := self._info(tx)) is not None}
            yield inst, {tx: i.stage1 for tx, i in infos.items()}, {tx: i.ev.amount for tx, i in infos.items()}

    def _info(self, tx: str) -> Optional[_TxInfo]:
        i = self._recent.get(tx)
        return i if i is not None else self._pinned.get(tx)

    def _has_open_high(self, account: str) -> bool:
        cases = self.cases.cases
        return any(tier_rank(cases[c].tier) >= _HIGH for c in self.cases.account_to_cases.get(account, ()))

    def _by_src(self, case: Case) -> dict[str, set[str]]:
        """Case tx_ids grouped by source account (Profile_Store keys DNA by ``src``)."""
        out: dict[str, set[str]] = {}
        unknown: set[str] = set()
        for tx in case.tx_ids:
            i = self._info(tx)
            if i is None:
                unknown.add(tx)
            else:
                out.setdefault(i.ev.src, set()).add(tx)
        if unknown:  # not cached: offer them to every known account of the Case
            known = self.profiles.accounts()
            for a in case.accounts:
                if a in known:
                    out.setdefault(a, set()).update(unknown)
        return out

    def _on_high(self, case: Case) -> None:
        """CaseManager hook: a Case first reached HIGH -> withhold its events (design §7)."""
        for src, txs in sorted(self._by_src(case).items()):
            self.profiles.withhold(src, txs)

    def _evict_recent(self) -> None:
        cutoff = self.graph.cutoff
        if cutoff is None:
            return
        heap = self._recent_heap
        while heap and heap[0][0] < cutoff:
            _, tx = heapq.heappop(heap)
            self._recent.pop(tx, None)

    # Evidence_Chains -------------------------------------------------------------------

    def _pin(self, txs: Iterable[str]) -> None:
        for tx in txs:
            if tx not in self._pinned and (i := self._recent.get(tx)) is not None:
                self._pinned[tx] = i

    def _update_chains(self, changes: Sequence[CaseChange]) -> list[str]:
        """Extend the Evidence_Chain of every touched Case with the entries of the events and
        Motif instances attached since its last update (plus its current stage-2 entry), and
        union merged Cases' chains into the survivor. O(new evidence), not O(Case size):
        this gives the same chain as rebuilding it from the whole Case and merging the
        rebuild into the previous chain, because existing entries always win (append-only)."""
        touched = list(dict.fromkeys(ch.case_id for ch in changes))
        merges = {ch.case_id: ch.merged_from for ch in changes if ch.merged_from}
        for cid in touched:
            case = self.cases.cases[cid]
            delta = self.cases.drain_delta(cid)
            b = self.chains.builder(cid)
            if not case.is_open and b is not None:
                continue  # closed (merged or labelled): chain is final
            if b is None:  # first chain of this Case: everything it holds
                b = self.chains.new_builder(cid)
                txs, mids = sorted(case.tx_ids), sorted(case.motifs)
            elif delta is not None:
                txs, mids = sorted(delta[0]), delta[1]
            else:
                txs, mids = [], []
            self._pin(txs)
            added = b.add(self._chain_entries(case, txs, mids))
            for loser in merges.get(cid, ()):
                lb = self.chains.builder(loser)
                if lb is not None:
                    added += b.add(lb.entries())
            if self.soc_enabled and added:
                self._kc_new.setdefault(cid, []).extend(added)
        return touched

    def _chain_entries(self, case: Case, txs: Sequence[str], mids: Sequence[str]) -> list[EvidenceEntry]:
        """Entries for the given events and Motif instances of ``case`` and its stage-2 entry."""
        hc = self.cfg.fusion.high_conflict
        edges: Optional[dict[str, tuple[datetime, str]]] = None

        def edge_of(tx: str) -> Optional[tuple[datetime, str]]:
            nonlocal edges  # built only for events with no cached info (rare)
            if edges is None:
                edges = {}
                for m in case.motifs.values():
                    for e in m.edges:
                        edges.setdefault(e.tx_id, (e.ts, e.currency))
            return edges.get(tx)

        def ts_ccy(tx: str) -> tuple[datetime, Optional[str]]:
            i = self._info(tx)
            if i is not None:
                return i.ev.ts, i.ev.currency
            ec = edge_of(tx)
            if ec is None:
                raise ValueError(f"evidence chain build: no timestamp for tx {tx!r}")
            return ec

        out: list[EvidenceEntry] = []
        for tx in txs:
            i = self._info(tx)
            fa = case.assessments.get(tx)
            if fa is None and i is None:
                continue
            ts, ccy = ts_ccy(tx)
            amt = case.amounts.get(tx) if fa is not None else None
            out.extend(tx_entries(tx, ts, fa, i.results if i is not None else (), hc, amount=amt, currency=ccy))
            if i is None:
                continue
            if i.lateness.late:
                out.append(EvidenceEntry(i.ev.ts, tx, LATE_ID, EntryKind.LATE, detail={
                    "lateness_s": float(i.lateness.seconds), "received_at": i.received_at.isoformat()}))
            for t in sorted(i.truncated, key=lambda t: t.value):
                out.append(EvidenceEntry(i.ev.ts, tx, f"{TRUNCATION_PREFIX}search:{t.value}",
                                         EntryKind.TRUNCATION, detail={"motif_type": t.value, "scope": "search"}))
            if i.cp is not None:
                out.append(EvidenceEntry(i.ev.ts, tx, CHANGE_POINT_ID, EntryKind.CHANGE_POINT, detail={
                    "classification": i.cp.outcome.value, "short_run_mass": float(i.cp.short_run_mass),
                    "est_start_tx": i.cp.est_start_tx}))
            out.extend(auth_entry(tx, link) for link in i.auth)
        if mids:
            s2w = stage2_weights(case.case_risk)
            for mid in mids:
                m = case.motifs[mid]
                ts_map = None
                if not m.edges:
                    last = max(m.tx_ids)
                    i = self._info(last)
                    ec = (i.ev.ts, None) if i is not None else edge_of(last)
                    ts_map = {last: ec[0]} if ec is not None else None
                out.extend(motif_entries(mid, m, s2w, ts_map))
        cr = case.case_risk
        if cr is not None and cr.stage2 is not None:
            e = stage2_entry(cr, self.cases.stage2_selection(case), ts_ccy(cr.stage2.tx_id)[0], hc)
            if e is not None:
                out.append(e)
        return out

    # Kill chain (Req 29) ------------------------------------------------------------------

    def kill_chain(self, case_id: str) -> Optional[KillChain]:
        """The Case's kill chain reconstructed from its current Evidence_Chain (any flag
        state; None without a chain)."""
        chain = self.chains.get(case_id)
        if chain is None:
            return None
        soc = self.cfg.soc
        return reconstruct(chain, soc.kill_chain_mapping, soc.kill_chain_default_stage)

    def _kill_chain_pass(self, changes: list[CaseChange], touched: list[str]) -> tuple[list[CaseChange], list[str]]:
        """Reconstruct the kill chain of every touched open Case; Cases whose most advanced
        stage changed are re-prioritised and their chains refreshed (one extra round)."""
        if not self.soc_enabled:
            return changes, touched
        soc = self.cfg.soc
        mapping, default = soc.kill_chain_mapping, soc.kill_chain_default_stage
        basis = (id(mapping), default)
        full = basis != self._kc_basis  # mapping changed: re-derive every touched Case once
        self._kc_basis = basis
        for cid in touched:
            case = self.cases.cases[cid]
            new = self._kc_new.pop(cid, ())
            if not case.is_open:
                continue
            if full or case.kill_chain is None:
                kc = self.kill_chain(cid)
            else:
                # Stages are a pure function of each entry and chains are append-only, so the
                # most advanced stage only moves when a new entry is beyond it: reconstruct then.
                top = max_stage(assign_stage(e, mapping, default).stage for e in new)
                cur = case.kill_chain_stage
                advanced = top is not None and (cur is None or stage_rank(top) > stage_rank(cur))
                kc = self.kill_chain(cid) if advanced else None
            if kc is not None:
                self.cases.set_kill_chain(cid, kc)
        more = self.cases.drain_changes()
        if not more:
            return changes, touched
        again = self._update_chains(more)
        return [*changes, *more], list(dict.fromkeys([*touched, *again]))

    def _extend_stage1_soc(self, ev: TransactionEvent, stage1: FusedAssessment, 
                          results: list[SensorResult], hc: float) -> tuple[FusedAssessment, list[SensorResult]]:
        """Fuse the Canary (Req 33.2) and Indicator (Req 34.3, 34.4) masses into stage 1.
        Only called when soc.enabled; unchanged when neither matches. Canary hits are recorded."""
        if self.canaries is not None:
            c = self.canaries.contribution(ev)
            if c is not None:
                self.canaries.observe(ev)
                stage1, _ = fuse([*stage1.contributions, c], hc, tx_id=stage1.tx_id, stage=1)
                refs = self.canaries.references(ev)
                feats = {"kind": "|".join(r.kind.value for r in refs), "field": "|".join(r.field for r in refs)}
                results = [*results, SensorResult(ev.tx_id, c.contributor_id, SensorOutcome.SUCCESS,
                                                  SensorOutput(c.mass, feats, None), 0.0)]
        # Documented hook of ``helix.soc.intel.attach_indicators``. Duck-typed: a stand-in store
        # (e.g. a Purple_Team copy implementing only ``copy()``) contributes nothing.
        ext = getattr(self.indicators, "extend_stage1", None)
        if ext is not None:
            stage1, results = ext(ev, stage1, results, high_conflict=hc)
        return stage1, list(results)

    def _is_canary_touch(self, ev: Any) -> bool:
        """True iff the event references a Canary (Req 33.3); False without Canaries."""
        return self.soc_enabled and self.canaries is not None and self.canaries.touches(ev)
    
    def _run_playbooks(self, case_ids: Sequence[str]) -> None:
        """Run PlaybookRunner on updated Cases (Req 32)."""
        if self.playbooks is None:
            return
        for cid in case_ids:
            case = self.cases.cases.get(cid)
            if case is None or not case.is_open:
                continue
            # Get techniques from the case's evidence chain
            chain = self.chains.get(cid)
            techniques = []
            if chain is not None:
                from helix.soc.techniques import case_techniques
                techniques = case_techniques(chain)
            # Call playbook runner
            try:
                self.playbooks.on_case_update(case, techniques=techniques)
            except Exception:  # noqa: BLE001 - playbook faults must not stop the pipeline
                log.exception("playbook runner failed for case %s", cid)

    # Storage ---------------------------------------------------------------------------

    def _persist_event(self, adm: AdmittedEvent, seq: int, s1: FusedAssessment, s2: FusedAssessment,
                       r: float, instances: Sequence[MotifInstance]) -> None:
        st = self.storage
        try:
            st.record_event(adm.event, seq_no=seq, late=adm.lateness.late,
                            lateness_s=float(adm.lateness.seconds), received_at=adm.received_at)
            st.record_assessment(s1, risk=r)
            st.record_assessment(s2, risk=risk(s2))
            for m in instances:
                st.upsert("motif_instances", {
                    "motif_id": m.motif_id, "motif_type": m.motif_type.value, "ts_start": m.ts_start,
                    "ts_end": m.ts_end, "tx_ids": sorted(m.tx_ids), "accounts": list(m.accounts),
                    "truncated": bool(m.truncated), "mass": [m.mass.f, m.mass.l, m.mass.t],
                })
        except Exception:  # noqa: BLE001 - Storage problems must not stop detection (Req 24)
            self.storage_errors += 1
            log.exception("storage write failed for %s", adm.event.tx_id)

    def _persist_cases(self, case_ids: Sequence[str], *, force: bool = False) -> None:
        """Write touched Cases now (first write, CLOSED, ``force``, or interval elapsed) or
        defer them: a Case that changes on every event is written at most once per
        ``case_persist_interval_s``, so persisting it does not cost O(Case size) per event."""
        now = self._mono()
        iv = self.case_persist_interval_s
        for cid in case_ids:
            last = self._persisted_at.get(cid)
            if force or last is None or now - last >= iv or not self.cases.cases[cid].is_open:
                self._write_case(cid)
            elif cid not in self._persist_pending:
                self._persist_pending.add(cid)
                self._persist_queue.append((last + iv, cid))

    def _persist_due(self) -> None:
        """Write deferred Cases whose interval has elapsed (queue is in due order)."""
        q = self._persist_queue
        if not q:
            return
        now = self._mono()
        while q and q[0][0] <= now:
            _, cid = q.popleft()
            if cid in self._persist_pending:
                self._write_case(cid)

    def _write_case(self, cid: str) -> None:
        st = self.storage
        self._persist_pending.discard(cid)
        self._persisted_at[cid] = self._mono()
        case = self.cases.cases[cid]
        try:
            row = self.case_summary(case)
            row.update(updated_at=case.updated_at, created_at=case.created_at, tx_ids=sorted(case.tx_ids),
                       closed_at=case.closed_at)
            row.pop("suggested_label", None)  # derived at serialisation, never stored
            st.upsert("cases", row)
            chain = self.chains.get(cid)
            if chain is not None:
                st.upsert("evidence_chains", {"case_id": cid, "updated_at": case.updated_at,
                                              "chain": dumps(chain)})
            done = self._history_saved.get(cid, 0)
            for n, h in enumerate(case.history[done:], start=done + 1):
                st.append("case_history", {"case_id": cid, "entry_no": n, "ts": h.ts, "action": h.action,
                                           "prev": h.prev, "new": h.new, "actor": h.actor})
            self._history_saved[cid] = len(case.history)
        except Exception:  # noqa: BLE001
            self.storage_errors += 1
            log.exception("storage write failed for case %s", cid)

    # Publishing ------------------------------------------------------------------------

    def _next_seq(self, channel: str) -> int:
        self._pub_seq[channel] += 1
        return self._pub_seq[channel]

    def _publish(self, channel: str, msg: dict[str, Any]) -> None:
        try:
            self.publisher.publish(channel, msg)
        except Exception:  # noqa: BLE001 - never block or break the worker
            log.exception("publish on %s failed", channel)

    def _publish_event(self, i: _TxInfo, r: float, case_id: Optional[str]) -> None:
        ev, fa = i.ev, i.stage1
        body: dict[str, Any] = {"tx_id": ev.tx_id, "ts": ev.ts.isoformat(), "src": ev.src, "dst": ev.dst,
                                "amount": str(ev.amount), "currency": ev.currency}
        for k in ("channel", "mcc", "lat", "lon", "device_id", "session_id"):
            v = getattr(ev, k)
            if v is not None:
                body[k] = v
        self._publish(EVENTS, {
            "channel": EVENTS, "type": "event.scored", "seq": self._next_seq(EVENTS), "event": body,
            "assessment": {"stage": 1, "belief": fa.belief, "plausibility": fa.plausibility,
                           "uncertainty": fa.uncertainty, "conflict": fa.conflict,
                           "high_conflict": fa.high_conflict},
            "stage2": {"belief": i.stage2.belief, "plausibility": i.stage2.plausibility,
                       "uncertainty": i.stage2.uncertainty, "risk": risk(i.stage2)},
            "risk": r, "late": i.lateness.late, "case_id": case_id,
            **({"warmup": True} if self._warmup else {}),
        })

    def _publish_cases(self, changes: Sequence[CaseChange]) -> None:
        backlog = len(self.prioritiser.backlog)  # O(1); backlog_ids() builds a list
        for cid in dict.fromkeys(ch.case_id for ch in changes):
            self._publish(CASES, {"channel": CASES, "type": "case.updated", "seq": self._next_seq(CASES),
                                  "case": self.case_summary(self.cases.cases[cid]), "backlog_count": backlog})

    def _maybe_publish_metrics(self) -> None:
        now = self.latency.now()
        if self._last_metrics is not None and now - self._last_metrics < self.metrics_interval_s:
            return
        self._last_metrics = now
        self.publish_metrics()

    def publish_metrics(self) -> None:
        self._publish(METRICS, {"channel": METRICS, "type": "metrics.latency", "seq": self._next_seq(METRICS),
                                "ts": self.clock.now().isoformat(), **self.metrics()})

    def _flush_case_changes(self) -> None:
        changes = self.cases.drain_changes()
        touched = self._update_chains(changes)
        changes, touched = self._kill_chain_pass(changes, touched)
        if self.playbooks is not None and self.soc_enabled:  # e.g. an escalation to CRITICAL
            self._run_playbooks(touched)
        if self.storage is not None:
            self._persist_cases(touched, force=True)  # investigator actions are written at once
        self._publish_cases(changes)

    def case_summary(self, case: Case) -> dict[str, Any]:
        at_risk: dict[str, Decimal] = {}
        edge_ccy: Optional[dict[str, str]] = None  # built once, only for events with no cached info
        for tx, amt in case.amounts.items():
            i = self._info(tx)
            if i is not None:
                ccy: Optional[str] = i.ev.currency
            else:
                if edge_ccy is None:
                    edge_ccy = {}
                    for m in case.motifs.values():
                        for e in m.edges:
                            edge_ccy.setdefault(e.tx_id, e.currency)
                ccy = edge_ccy.get(tx)
            if ccy is not None:
                at_risk[ccy] = at_risk.get(ccy, Decimal("0")) + amt
        cr = case.case_risk
        unc = None if cr is None else (cr.stage2.uncertainty if cr.stage2 is not None else cr.best_tx.uncertainty)
        soc: dict[str, Any] = {}
        if self.soc_enabled:  # extra keys only with cyber fusion on: existing payloads unchanged
            st = case.kill_chain_stage
            soc = {"kill_chain_stage": None if st is None else st.value,
                   "expected_loss_base": None if case.expected_loss_base is None else str(case.expected_loss_base)}
        return {
            "case_id": case.case_id, "status": case.status.value, "tier": case.tier.value, "risk": case.risk,
            "uncertainty": unc, "priority": str(case.priority), "expected_loss": str(case.expected_loss),
            "placement": case.placement.value,
            "amount_at_risk": [{"currency": c, "amount": str(a.quantize(_CENT))} for c, a in sorted(at_risk.items())],
            "accounts": sorted(case.accounts), "tx_count": len(case.tx_ids),
            # MotifTypes present = keys of the incrementally kept stage-2 selection (O(types))
            "motifs": sorted(m.motif_type.value for _, m in self.cases.stage2_selection(case)),
            "assignee": case.assignee, "merged_into": case.merged_into,
            "label": None if case.label is None else case.label.value,
            "updated_at": case.updated_at.isoformat(),
            # Req 41.1, 41.4: why a CLOSED Case closed, and the Label_Suggester's advice
            # (computed here, never stored; it never changes Case state).
            "closure_reason": None if case.closure_reason is None else case.closure_reason.value,
            "suggested_label": suggest_case(case, self.cfg.triage).to_dict(),
            **soc,
        }


def auth_entry(tx_id: str, link: AuthLink) -> EvidenceEntry:
    """AUTH_EVENT Evidence_Chain record of one correlated Auth_Event (Req 28.7, 28.8)."""
    a = link.event
    return EvidenceEntry(a.ts, tx_id, AUTH_PREFIX + a.event_id, EntryKind.AUTH_EVENT, "AUTH", detail={
        "event_id": a.event_id, "event_type": a.event_type.value, "account_id": a.account_id,
        "session_id": a.session_id, "device_id": a.device_id, "ip": a.ip,
        "payee_account_id": a.payee_account_id, "linked_by": link.linked_by or None,
        "signal": link.signal, "suspicious": link.suspicious, "new_device": link.record.new_device,
    })


__all__ = [
    "CASES",
    "CHANNELS",
    "CHANGE_POINT_ID",
    "EVENTS",
    "LATE_ID",
    "METRICS",
    "UNAVAILABLE",
    "EventOutcome",
    "FeedbackHook",
    "Helix",
    "InProcessPublisher",
    "LatencyRecorder",
    "LatencySummary",
    "NullPublisher",
    "Publisher",
    "Stage",
    "percentile",
    "summarise",
]
