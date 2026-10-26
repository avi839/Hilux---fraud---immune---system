"""AppState: the API_Server's composition of HELIX components plus its background tasks.

Background tasks (all on the serving event loop, started/stopped by the app lifespan):

- pipeline worker: ``Helix.run(ingestor)``; restarted if one event raises, so a bad event
  cannot stop detection;
- Storage writer and snapshotter (when Storage is configured);
- metrics ticker: publishes loop metrics on ``/ws/metrics`` every ``metrics_interval_s``
  (< 1 s, Req 23.7) while a client is subscribed, samples the ingestion rate and tracks the
  latency breach state (Req 25.5, 25.7);
- generator feeder (optional): moves a stream-time cursor forward by the elapsed wall time
  each tick and pushes every Synthetic_Generator event with ``ts <= cursor`` into the
  ingestor, so events arrive at ``generator.rate_eps`` with timestamps from the generator's
  SimClock schedule. Under backpressure the cursor stops (nothing is dropped);
- immune miss detection (when the Immune_System is enabled with a label reader): every
  ``immune_tick_s`` it records attack events whose detection deadline (stream time) has
  passed without them being Detected_Events, hands each miss to ``ImmuneSystem.on_miss``
  and runs negative selection (``ImmuneSystem.tick``);
- Attacker_Agent (optional, ``helix.attacker.attach_attacker``): its own tick task, started
  and stopped through ``start_attacker`` / ``stop_attacker`` and cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from helix.cases import Placement
from helix.config import HelixConfig
from helix.core import CASES, EVENTS, METRICS, UNAVAILABLE, Helix, InProcessPublisher, Stage
from helix.evaluation import EvaluationMonitor
from helix.ingest import StreamIngestor
from helix.replay import ReplayService
from helix.sim.clock import ManualClock, SimClock

from .hub import WSHub

log = logging.getLogger(__name__)

LATENCY_TARGET_MS = 50.0  # Req 25.1 / 25.7
RATE_WINDOW_S = 60.0
MAX_FEED_STEP_S = 1.0  # a stalled loop does not cause a catch-up burst larger than 1 s


@dataclass
class AppState:
    cfg: HelixConfig
    clock: SimClock
    ingestor: StreamIngestor
    helix: Helix
    publisher: InProcessPublisher
    hub: WSHub
    monitor: Optional[EvaluationMonitor] = None
    replay: Optional[ReplayService] = None
    storage: Any = None  # helix.storage.Storage, opened
    generator: Any = None  # helix.sim.SyntheticGenerator
    attacker: Any = None  # helix.attacker.AttackerRunner; None until attached
    immune: Any = None  # helix.immune.ImmuneSystem; None when disabled
    miss_detector: Any = None  # helix.immune.miss_detector.MissDetector (needs gt_reader)
    gt_writer: Any = None  # label producer side (generator / attacker)
    gt_reader: Any = None  # label read side, given only to the Evaluation_Monitor / MissDetector
    attacker_seed: Optional[int] = None
    attacker_autostart: bool = False
    immune_tick_s: float = 0.5
    metrics_interval_s: float = 0.5
    generator_tick_s: float = 0.05
    generator_autostart: bool = False
    close_storage_on_shutdown: bool = True
    # WebSocket handshakes without an Origin header are rejected unless this is set (tests,
    # non-browser clients); a present-but-wrong Origin is always rejected (Req 27.4).
    allow_missing_ws_origin: bool = False
    # SOC components (Req 30-37), built by ``enable_soc``. When left None the SOC routers create
    # in-memory defaults on mount (see helix.api.soc_*_routes); the Rule_Engine lives on
    # ``helix.rules``.
    playbook_runner: Any = None  # helix.soc.playbooks.PlaybookRunner
    canaries: Any = None  # helix.soc.canaries.CanaryRegistry
    canary_campaign_of: Any = None  # optional event_id -> campaign_id (canary hit report)
    indicators: Any = None  # helix.soc.intel.IndicatorStore
    ledger: Any = None  # helix.soc.ledger.AuditLedger
    soc_console: Any = None  # helix.soc.console.SocConsole
    purple: Any = None  # helix.soc.purple.PurpleTeam
    resilience: Any = None  # helix.soc.resilience.ResilienceMode
    dataset: Any = None  # helix.datasets.DatasetFeeder (--dataset); None = DISABLED (Req 39)
    # Investigator workload (Req 41): Auto_Triage (None unless triage.enabled) and the
    # WorkloadMeter behind the "workload" block of GET /metrics. See the Auto_Triage block.
    triage: Any = None  # helix.cases.triage.AutoTriage
    workload: Any = None  # helix.cases.workload.WorkloadMeter
    triage_tick_s: float = 0.5  # wall-clock poll; sweeps happen on stream-time boundaries
    _triage_next: Optional[datetime] = None  # next stream-time sweep boundary
    # Demo liveliness (task 31.1). Generator warm-up: seconds of simulated generator traffic
    # run in Warm_Up_Mode before the live feeder starts (0 = off). Simulated investigator:
    # helix.evaluation.sim_investigator.SimInvestigator (None = disabled, the default).
    generator_warmup_s: float = 0.0
    generator_warmup_report: Any = None  # helix.datasets.WarmupReport once the warm-up finished
    generator_warmup_state: str = "off"  # off | pending | running | done | failed
    sim_investigator: Any = None

    worker_errors: int = 0
    ws_origin_rejected: int = 0  # WebSocket handshakes closed with 1008 (bad/missing Origin)
    body_too_large: int = 0  # requests answered 413 by the body size limit
    breach_count: int = 0
    breach_active: bool = False
    started_at: Optional[float] = None
    _tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    _rate_samples: deque = field(default_factory=deque)
    _unsubs: list = field(default_factory=list)
    _admitted: set = field(default_factory=set)  # case ids reported to the monitor
    _gen_cursor: Optional[datetime] = None
    _detected_snap: frozenset = field(default_factory=frozenset)
    _attack_kinds: dict = field(default_factory=dict)  # tx_id -> Pattern_Genome type (antibody tags)

    # --- construction ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        cfg: Optional[HelixConfig] = None,
        clock: Optional[SimClock] = None,
        *,
        storage: Any = None,
        monitor: Optional[EvaluationMonitor] = None,
        generator: Any = None,
        attacker: Any = None,
        hub: Optional[WSHub] = None,
        learner: bool = True,
        immune: Optional[bool] = None,
        gt_writer: Any = None,
        gt_reader: Any = None,
        restore_state: bool = True,
        soc: Optional[bool] = None,
        **helix_kw: Any,
    ) -> AppState:
        """Wire a Helix pipeline, ingestor, publisher, hub, (with Storage) replay and, when
        ``helix.learn`` is importable, the Feedback_Learner. ``immune`` (default:
        ``cfg.immune.enabled``) attaches the Immune_System; with ``gt_reader`` its miss
        detection runs as a background task. ``soc`` (default: ``cfg.soc.enabled``) turns
        cyber fusion on (``soc.enabled`` is forced true in the config) and builds the SOC
        components with ``enable_soc``.
        
        When ``restore_state`` is True (default) and Storage is configured, restores
        persisted state (DNA, CPD, Cases, weights) from Storage on startup (Req 24.6)."""
        cfg = cfg if cfg is not None else HelixConfig()
        if soc and not cfg.soc.enabled:
            # The pipeline reads soc.enabled at construction, so the flag is set first.
            cfg = cfg.model_copy(update={"soc": cfg.soc.model_copy(update={"enabled": True})})
        clock = clock if clock is not None else ManualClock(datetime.now(timezone.utc))
        pub = InProcessPublisher()
        
        # Restore state before creating Helix if Storage is available (Req 24.6)
        restored = None
        if storage is not None and restore_state:
            restored = storage.restore()
            log.info(
                "restoring state: %d cases, latest event %s, snapshot=%s, last_seq_no=%s",
                len(restored.cases), restored.latest_event_ts,
                "present" if restored.snapshot is not None else "none",
                restored.last_seq_no,
            )
        
        helix = Helix(cfg, clock, storage=storage, publisher=pub, **helix_kw)
        
        # Apply restored state to Helix components
        if restored is not None:
            _apply_restored_state(helix, restored, clock, storage)
        
        if learner:
            attach_learner(helix)
            # Restore weight history if learner is attached
            if restored is not None and helix.learner is not None:
                helix.learner.restore_live()
        
        rejected = None
        if storage is not None:
            from helix.storage import DuckDBRejectedLog

            rejected = DuckDBRejectedLog(storage)
        ingestor = StreamIngestor(cfg.ingest, clock, rejected)
        replay = None
        if storage is not None:
            replay = ReplayService(storage, helix.cases.cases, cfg, graph=helix.graph)
        state = cls(cfg=cfg, clock=clock, ingestor=ingestor, helix=helix, publisher=pub,
                    hub=hub if hub is not None else WSHub(), monitor=monitor, replay=replay,
                    storage=storage, generator=generator, attacker=attacker, gt_writer=gt_writer,
                    gt_reader=gt_reader)
        state.wire()
        state.enable_workload()
        if cfg.triage.enabled:
            state.enable_triage()
        if immune if immune is not None else cfg.immune.enabled:
            state.enable_immune()
        if soc if soc is not None else cfg.soc.enabled:
            state.enable_soc()
        return state

    def wire(self) -> None:
        """Subscribe the WS hub (and the Evaluation_Monitor) to the pipeline channels."""
        pub, hub = self.publisher, self.hub
        self._unsubs.append(pub.subscribe(EVENTS, lambda m: hub.publish(EVENTS, m)))
        self._unsubs.append(pub.subscribe(CASES, lambda m: hub.publish(CASES, m)))
        self._unsubs.append(pub.subscribe(METRICS, self._on_metrics))
        if self.monitor is not None:
            self._unsubs.append(pub.subscribe(EVENTS, self._monitor_event))
            self._unsubs.append(pub.subscribe(CASES, self._monitor_case))
            self.helix.prioritiser.period_hooks.append(self.monitor.on_period)

    @property
    def learner(self) -> Any:
        return getattr(self.helix, "learner", None)

    # --- SOC ------------------------------------------------------------------------------

    @property
    def audit_ledger(self) -> Any:
        """Alias of ``ledger`` (the name the playbook routes look up)."""
        return self.ledger

    @audit_ledger.setter
    def audit_ledger(self, value: Any) -> None:
        self.ledger = value

    @property
    def soc_enabled(self) -> bool:
        return bool(getattr(self.helix, "soc_enabled", False))

    def enable_soc(self) -> dict[str, Any]:
        """Build the SOC components over the pipeline (idempotent; needs ``soc.enabled``,
        Req 30-37). With Storage the Audit_Ledger, Indicator_Store, handoff notes and
        Exercise_Scorecards are persisted in DuckDB and reloaded here; the Canary set is
        regenerated deterministically from the generator seed. Components already present
        on the state are kept. Returns the components by name.

        Connected to the pipeline only through the attach helpers the routers use:
        ``helix.rules`` (``attach_rules``), ``helix.indicators`` (``attach_indicators``) and
        ``helix.ledger`` (``attach_audit``)."""
        if not self.soc_enabled:
            raise ValueError("SOC components need cyber fusion (soc.enabled = true)")
        from helix.soc.canaries import CanaryCounts, CanaryRegistry
        from helix.soc.console import SocConsole
        from helix.soc.intel import IndicatorConfig, IndicatorStore, attach_indicators
        from helix.soc.ledger import AuditLedger, attach_audit
        from helix.soc.playbooks import PlaybookRunner
        from helix.soc.purple import PurpleTeam
        from helix.soc.resilience import ResilienceMode
        from helix.soc.rules import attach_rules, rule_engine_for

        sc, helix, storage = self.cfg.soc, self.helix, self.storage
        if self.ledger is None:  # first: the playbook runner audits into it
            self.ledger = AuditLedger(storage=storage)
        if getattr(helix, "ledger", None) is not self.ledger:
            attach_audit(helix, self.ledger)
        if self.indicators is None:
            icfg = IndicatorConfig(default_confidence=int(sc.indicator_default_confidence),
                                   default_expiry_days=float(sc.indicator_default_expiry_days),
                                   base_mass=float(sc.indicator_base_mass))
            self.indicators = IndicatorStore(icfg, storage=storage)
        if getattr(helix, "indicators", None) is not self.indicators:
            attach_indicators(helix, self.indicators)
        if getattr(helix, "rules", None) is None:
            eng = attach_rules(helix, rule_engine_for(helix, storage), storage=storage)
            if sc.rules_dir:
                for res in eng.load_directory(sc.rules_dir):
                    if res.status == "rejected":
                        log.warning("Detection_Rule %s rejected: %s", res.path,
                                    "; ".join(str(p) for p in res.problems))
        if self.canaries is None:
            seed = getattr(self.generator, "seed", None)
            if not isinstance(seed, int) or isinstance(seed, bool):
                seed = int(self.cfg.generator.default_seed)
            counts = CanaryCounts(int(sc.canary_accounts), int(sc.canary_payees), int(sc.canary_devices))
            self.canaries = CanaryRegistry.generate(seed, counts)
        if self.playbook_runner is None:
            self.playbook_runner = PlaybookRunner.with_defaults(
                approval_threshold=sc.playbook_approval_threshold, dry_run=bool(sc.playbook_dry_run),
                audit=self.ledger, clock=self.clock.now)
        if self.soc_console is None:
            self.soc_console = SocConsole(helix, storage=storage)
        if self.purple is None:
            self.purple = PurpleTeam(helix, storage=storage, indicators=lambda: self.indicators,
                                     attacker=lambda: self.attacker)
        if self.resilience is None:
            self.resilience = ResilienceMode()  # Resilience_Mode is disabled until switched on
        self._wire_soc_pipeline()
        return self.soc_components()

    def _wire_soc_pipeline(self) -> None:
        """Connect the SOC components to the detection loop (task 18.2, idempotent): Canaries
        (stage-1 mass, forced Cases, Prioritiser exception, DNA / Self_Set exclusion), the
        Playbook_Runner on Case updates and the Audit_Ledger for escalations, weight, rule
        and config changes. Feedback_Labels reach the ledger only through the
        ``attach_audit`` feedback hook, so each label is recorded once."""
        from helix.soc.ledger import attach_weight_audit

        helix = self.helix
        helix.audit = self.ledger
        helix.playbooks = self.playbook_runner
        if self.canaries is not None and helix.canaries is not self.canaries:
            helix.canaries = self.canaries
            pred = self.canaries.predicate()
            helix.profiles.set_canary_check(pred)
            if self.immune is not None:
                self.immune.is_canary = pred
        rules = getattr(helix, "rules", None)
        if rules is not None and hasattr(rules, "audit"):
            rules.audit = self.ledger
        if self.learner is not None and self.ledger is not None:
            attach_weight_audit(self.learner, self.ledger)

    def soc_components(self) -> dict[str, Any]:
        return {"rules": getattr(self.helix, "rules", None), "playbook_runner": self.playbook_runner,
                "canaries": self.canaries, "indicators": self.indicators, "ledger": self.ledger,
                "soc_console": self.soc_console, "purple": self.purple, "resilience": self.resilience}

    # --- immune system ------------------------------------------------------------------

    def enable_immune(self, seed: Optional[int] = None) -> Any:
        """Attach an Immune_System to the pipeline (idempotent) and, when a label reader is
        configured, a MissDetector fed with every published event. Returns the system."""
        if self.immune is not None:
            return self.immune
        from helix.immune import ImmuneSystem, attach_immune

        icfg = self.cfg.immune
        if not icfg.enabled:
            icfg = icfg.model_copy(update={"enabled": True})
        if seed is None:
            seed = int(self.cfg.generator.default_seed)
        canaries = getattr(self.helix, "canaries", None)  # SOC enabled first: exclude Canaries
        imm = ImmuneSystem(icfg, self.clock, seed=seed, tagger=self._attack_kind,
                           is_canary=None if canaries is None else canaries.predicate())
        attach_immune(self.helix, imm)
        self.immune = imm
        if self.gt_reader is not None:
            from helix.immune.miss_detector import MissDetector

            self.miss_detector = MissDetector(self.gt_reader, self._is_detected, imm.on_miss)
            self._unsubs.append(self.publisher.subscribe(EVENTS, self._observe_for_misses))
        return imm

    def _observe_for_misses(self, msg: Mapping[str, Any]) -> None:
        ev = msg["event"]
        self.miss_detector.observe(ev["tx_id"], datetime.fromisoformat(ev["ts"]))

    def _is_detected(self, tx_id: str) -> bool:
        """Detected_Event check for the MissDetector: membership in an admitted Case, from the
        Evaluation_Monitor snapshot taken at the start of the step (or, without a monitor,
        the Case_Manager's admitted Cases)."""
        if self.monitor is not None:
            return tx_id in self._detected_snap
        cid = self.helix.cases.case_of(tx_id)
        case = None if cid is None else self.helix.cases.cases.get(cid)
        return case is not None and case.admitted_period is not None

    def _attack_kind(self, tx_id: str) -> list[str]:
        """Antibody tagger (Req 21.13): the Pattern_Genome type of the attacker campaign that
        planned ``tx_id``. Metadata only; it never changes how an antibody matches."""
        kind = self._attack_kinds.get(tx_id)
        if kind is None and self.attacker is not None:
            try:
                for c in self.attacker.campaigns():
                    pt = self.attacker.genome(c.genome_id).ptype
                    for e in c.event_ids:
                        self._attack_kinds[e] = str(getattr(pt, "value", pt))
            except Exception:  # noqa: BLE001 - tags are best effort
                log.exception("immune tagger: attacker lookup failed")
            kind = self._attack_kinds.get(tx_id)
        return [] if kind is None else [kind]

    def immune_step(self, now: Optional[datetime] = None) -> list[Any]:
        """One miss-detection pass: record attack events whose deadline (``now`` minus
        ``immune.detection_deadline_s``, stream time) has passed undetected, then run
        negative selection. Returns the misses recorded by this pass."""
        imm = self.immune
        if imm is None:
            return []
        now = self.clock.now() if now is None else now
        out: list[Any] = []
        det = self.miss_detector
        if det is not None:
            if self.monitor is not None:
                self._detected_snap = self.monitor.detected_events()
            deadline = now - timedelta(seconds=float(imm.cfg.detection_deadline_s))
            out = det.record_misses(deadline, now=now)
        imm.tick(now)
        return out

    async def _immune_loop(self) -> None:
        while True:
            await asyncio.sleep(self.immune_tick_s)
            try:
                self.immune_step()
            except Exception:  # noqa: BLE001 - one bad pass must not stop miss detection
                log.exception("immune miss detection failed")

    # --- Auto_Triage and workload (Req 41) --------------------------------------------------
    #
    # Self-contained: the WorkloadMeter counts stored Feedback_Labels through its own feedback
    # hook and AUTO_CLEARED closures through the AutoTriage ``on_clear`` callback. The triage
    # task polls on the wall clock but sweeps only when stream time has passed the next
    # ``triage.sweep_interval_s`` boundary. ``triage_step`` is synchronous and runs on the same
    # event loop as the pipeline worker, between events (never mid-event). It never calls
    # ``Helix.label``, so the feedback hooks never see an AUTO_CLEARED closure (Req 41.2).

    def enable_workload(self) -> Any:
        """Attach the WorkloadMeter (idempotent): one count per stored Feedback_Label."""
        if self.workload is None:
            from helix.cases.workload import WorkloadMeter

            self.workload = WorkloadMeter()
        hooks = self.helix.feedback_hooks
        if not any(getattr(h, "workload_meter", None) is self.workload for h in hooks):
            hooks.append(self.workload.feedback_hook())
        return self.workload

    def enable_triage(self) -> Any:
        """Build the AutoTriage over the Case_Manager (idempotent). It audits into the SOC
        Audit_Ledger when one exists at sweep time (Req 41.3)."""
        if self.triage is None:
            from helix.cases.triage import AutoTriage

            meter = self.enable_workload()
            self.triage = AutoTriage(self.cfg.triage, self.helix.cases, self.ledger,
                                     on_clear=lambda _case: meter.on_auto_clear())
        return self.triage

    def triage_step(self, now: Optional[datetime] = None, *, force: bool = False) -> list[str]:
        """Sweep when stream time ``now`` has reached the next sweep boundary (or ``force``).
        Publishes / persists the closures like any Case change. Returns the closed ids."""
        tri = self.triage
        if tri is None:
            return []
        now = self.clock.now() if now is None else now
        if not force and self._triage_next is not None and now < self._triage_next:
            return []
        self._triage_next = now + timedelta(seconds=float(self.cfg.triage.sweep_interval_s))
        tri.audit = self.ledger  # the SOC ledger may have been built after the triage
        closed = tri.sweep(now)
        if closed:
            self.helix._flush_case_changes()  # CaseChange(action="auto_clear") on /ws/cases
        return closed

    async def _triage_loop(self) -> None:
        while True:
            await asyncio.sleep(self.triage_tick_s)
            try:
                self.triage_step()
            except Exception:  # noqa: BLE001 - one bad sweep must not stop triage
                log.exception("auto-triage sweep failed")

    def workload_metrics(self) -> dict[str, Any]:
        """The ``workload`` block of GET /metrics (Req 41.10)."""
        return {**self.enable_workload().snapshot(), "sim_investigator": self.sim_investigator_status()}

    # --- attacker -----------------------------------------------------------------------

    def ensure_attacker(self) -> Any:
        """Attach the Attacker_Agent on first use (needs the Synthetic_Generator's account
        pool). Raises LookupError without a generator."""
        if self.attacker is not None:
            return self.attacker
        if self.generator is None:
            raise LookupError("the Attacker_Agent needs a Synthetic_Generator for its account pool")
        from helix.attacker import attach_attacker

        return attach_attacker(self, seed=self.attacker_seed, writer=self.gt_writer)

    def attacker_running(self) -> bool:
        a = self.attacker
        return bool(a is not None and getattr(a, "running", False))

    def start_attacker(self) -> bool:
        """Start the attacker tick task; returns False if it was already running."""
        return bool(self.ensure_attacker().start())

    def stop_attacker(self) -> bool:
        """Stop the attacker tick task; returns False if it was not running."""
        a = self.attacker
        return bool(a is not None and a.stop())

    # --- monitor wiring -----------------------------------------------------------------

    def _monitor_event(self, msg: Mapping[str, Any]) -> None:
        ev = msg["event"]
        self.monitor.on_event_ingested(ev["tx_id"], Decimal(ev["amount"]), datetime.fromisoformat(ev["ts"]))

    def _monitor_case(self, msg: Mapping[str, Any]) -> None:
        """Report admissions: a Case whose placement became ACTIVE, and every later change
        of an already-admitted Case (it may have gained events, e.g. by a merge)."""
        c = msg["case"]
        cid = c["case_id"]
        if c.get("placement") != Placement.ACTIVE.value and cid not in self._admitted:
            return
        case = self.helix.cases.cases.get(cid)
        if case is None:
            return
        self._admitted.add(cid)
        self.monitor.on_case_admitted(cid, sorted(case.tx_ids), self.clock.now(), motifs=c.get("motifs") or ())

    # --- runtime config -----------------------------------------------------------------

    def apply_runtime_config(self, candidate: HelixConfig, changes: Mapping[str, Any]) -> None:
        """Queue validated prioritiser changes for the next budget period (Req 14.10)."""
        for path, value in changes.items():
            section, name = path.split(".", 1)
            if section != "prioritiser":  # pragma: no cover - only prioritiser fields are runtime
                raise LookupError(path)
            self.helix.prioritiser.request_change(name, value)
            if self.soc_enabled and self.ledger is not None:  # Req 35.1 CONFIG_CHANGE
                from helix.soc.ledger import audit_config_change

                audit_config_change(self.ledger, "api", path, getattr(self.cfg.prioritiser, name, None), value,
                                    effective="next_period")
        self.cfg = candidate

    # --- metrics ------------------------------------------------------------------------

    def _on_metrics(self, msg: Mapping[str, Any]) -> None:
        if self.hub.subscribers(METRICS):
            self.hub.publish(METRICS, {**msg, "stages": self.stage_means(), "ingest": self.ingest_stats(),
                                       "latency_breach": self.breach()})

    def stage_means(self) -> dict[str, dict[str, Any]]:
        """Event count and mean latency (ms) per loop stage over the rolling window (Req 23.7)."""
        rec = self.helix.latency
        out: dict[str, dict[str, Any]] = {}
        now = rec.now()
        for s in Stage:
            rec.window(s, now)  # evicts expired samples
            samples = [ms for _, ms in getattr(rec, "_win", {}).get(s, ())]
            out[s.value] = {
                "count": len(samples),
                "mean_ms": (sum(samples) / len(samples)) if samples else UNAVAILABLE,
                "total": rec.all_time(s).count,
            }
        return out

    def ingest_rate(self) -> float:
        """Accepted events per second over the last 60 s (sampled, wall clock)."""
        now = time.monotonic()
        d = self._rate_samples
        d.append((now, self.ingestor.counters.accepted))
        while len(d) > 2 and d[1][0] <= now - RATE_WINDOW_S:
            d.popleft()
        (t0, a0), (t1, a1) = d[0], d[-1]
        return (a1 - a0) / (t1 - t0) if t1 > t0 else 0.0

    def ingest_stats(self) -> dict[str, Any]:
        out: dict[str, Any] = asdict(self.ingestor.counters)
        out.update(queue_length=len(self.ingestor), backpressure=self.ingestor.backpressure,
                   backpressure_limit=self.ingestor.config.backpressure_limit, rate_eps=self.ingest_rate())
        return out

    def breach(self) -> dict[str, Any]:
        """Rolling-window p95 end-to-end latency against the 50 ms target (Req 25.7)."""
        p95 = self.helix.latency.window(Stage.END_TO_END).p95
        over = p95 is not None and p95 > LATENCY_TARGET_MS
        if over and not self.breach_active:
            self.breach_count += 1
        self.breach_active = over
        return {"active": over, "count": self.breach_count, "threshold_ms": LATENCY_TARGET_MS,
                "p95_ms": UNAVAILABLE if p95 is None else p95}

    # --- lifecycle ------------------------------------------------------------------------

    def running(self, name: str) -> bool:
        t = self._tasks.get(name)
        return t is not None and not t.done()

    async def start(self) -> None:
        self.hub.bind()
        self.started_at = time.monotonic()
        self._spawn("worker", self._worker())
        self._spawn("metrics", self._metrics_loop())
        st = self.storage
        if st is not None:
            self._spawn("storage_writer", st.writer())
            self._spawn("storage_snapshotter", st.snapshotter(self._snapshot_source))
        warm = (self.generator is not None and self.generator_autostart and self.dataset is None
                and self.generator_warmup_s > 0)
        if self.generator is not None and self.generator_autostart and self.dataset is None and not warm:
            self.start_generator()
        self.start_dataset()
        if self.immune is not None:
            self._spawn("immune", self._immune_loop())
        if self.triage is not None:
            self._spawn("triage", self._triage_loop())
        if self.sim_investigator is not None:
            self._spawn("sim_investigator", self._sim_investigator_loop())
        if warm:
            # The warm-up runs as a startup task (the server answers meanwhile); the live
            # feeder and the attacker start from its end.
            self.generator_warmup_state = "pending"
            self._spawn("generator_warmup", self._warmup_then_start())
        elif self.attacker_autostart:
            self._autostart_attacker()

    def _autostart_attacker(self) -> None:
        try:
            self.start_attacker()
        except LookupError:
            log.warning("attacker autostart skipped: no Synthetic_Generator configured")

    async def stop(self) -> None:
        await self._cancel("generator_warmup")
        a = self.attacker
        if a is not None and hasattr(a, "aclose"):
            await a.aclose()
        await self._cancel("sim_investigator")
        await self._cancel("immune")
        await self._cancel("triage")
        await self.stop_dataset()
        await self._cancel("generator")
        self.helix.stop()
        for name in ("metrics", "worker"):
            await self._cancel(name)
        st = self.storage
        if st is not None:
            tasks = [t for n in ("storage_writer", "storage_snapshotter") if (t := self._tasks.pop(n, None))]
            if self.close_storage_on_shutdown:
                try:
                    await st.shutdown(final_snapshot=self._snapshot_source, tasks=tasks)
                except Exception:  # noqa: BLE001 - shutdown must finish
                    log.exception("storage shutdown failed")
            else:
                st.stop()
                await asyncio.gather(*tasks, return_exceptions=True)

    def _snapshot_source(self) -> tuple[Any, Any, datetime]:
        dna, cpd = self.helix.snapshot_state()
        return dna, cpd, self.clock.now()

    def _spawn(self, name: str, coro: Any) -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro, name=f"helix-{name}")
        self._tasks[name] = t
        return t

    async def _cancel(self, name: str) -> None:
        t = self._tasks.pop(name, None)
        if t is None:
            return
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t

    async def _worker(self) -> None:
        while True:
            try:
                await self.helix.run(self.ingestor)
                return  # stop() was called
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad event must not stop the loop
                self.worker_errors += 1
                log.exception("pipeline worker: event processing failed; continuing")

    async def _metrics_loop(self) -> None:
        while True:
            await asyncio.sleep(self.metrics_interval_s)
            try:
                self.ingest_rate()
                self.breach()
                if self.hub.subscribers(METRICS):
                    self.helix.publish_metrics()
            except Exception:  # noqa: BLE001
                log.exception("metrics tick failed")

    # --- generator ------------------------------------------------------------------------

    # --- dataset feeder (Req 39, 40; design §24) -----------------------------------------
    # The Dataset_Feeder runs as the background task "dataset" beside the generator feeder.
    # It submits through the same Stream_Ingestor (with backpressure) and holds only the
    # ground-truth writer. While it is set the Synthetic_Generator never starts (Req 39.8).

    def dataset_active(self) -> bool:
        return self.dataset is not None

    def start_dataset(self) -> bool:
        """Spawn the Dataset_Feeder task (once); False without ``--dataset`` or if spawned."""
        if self.dataset is None or "dataset" in self._tasks:
            return False
        self._spawn("dataset", self.dataset.run())
        return True

    async def stop_dataset(self) -> None:
        await self._cancel("dataset")

    # --- generator ------------------------------------------------------------------------

    def start_generator(self) -> bool:
        """Start the feeder; returns False if it was already running."""
        if self.dataset is not None:
            raise RuntimeError("dataset streaming is active")  # 409 via helix.api.dataset_routes
        if self.generator is None:
            raise LookupError("no Synthetic_Generator configured")
        if self.running("generator"):
            return False
        self._spawn("generator", self._feed())
        return True

    def stop_generator(self) -> bool:
        """Stop the feeder; returns False if it was not running."""
        t = self._tasks.pop("generator", None)
        if t is None or t.done():
            return False
        t.cancel()
        return True

    async def _feed(self) -> None:
        gen = self.generator
        if self._gen_cursor is None:
            self._gen_cursor = gen.t0
        last = time.monotonic()
        while True:
            await asyncio.sleep(self.generator_tick_s)
            now = time.monotonic()
            step, last = min(now - last, MAX_FEED_STEP_S), now
            if self.ingestor.backpressure:
                continue  # pause stream time; nothing is dropped
            self._gen_cursor += timedelta(seconds=step)
            for ev in gen.events_until(self._gen_cursor):
                self.ingestor.submit(ev)

    # --- generator warm-up (task 31.1) ------------------------------------------------------
    #
    # ``seconds`` of the generator's own schedule are pushed through the Stream_Ingestor as
    # fast as backpressure allows while ``helix.warmup`` is set (Warm_Up_Mode, Req 40.7):
    # Behavioural_DNA, Change_Point_Detector and Temporal_Graph state are built, no Case is
    # created. Ground-truth labels are written by the generator as usual (it holds only the
    # writer). The stream-time clock ends at the warm-up end and the live feeder continues
    # from that cursor, so the warm-up history is simply the past of the live stream.

    async def run_generator_warmup(self, seconds: float, *, chunk_s: float = 5.0,
                                   log_every_s: float = 5.0) -> Any:
        """Run the warm-up (needs the pipeline worker running). Returns the WarmupReport."""
        from helix.datasets.warmup import build_report

        gen = self.generator
        if gen is None:
            raise LookupError("no Synthetic_Generator configured")
        if self._gen_cursor is None:
            self._gen_cursor = gen.t0
        end = self._gen_cursor + timedelta(seconds=float(seconds))
        step = timedelta(seconds=float(chunk_s))
        ing = self.ingestor
        self.generator_warmup_state = "running"
        self.helix.warmup = True
        t0 = last_log = time.monotonic()
        n = 0
        log.info("generator warm-up: %.0f s of simulated traffic (until %s)", seconds, end.isoformat())
        try:
            while self._gen_cursor < end:
                self._gen_cursor = min(end, self._gen_cursor + step)
                for ev in gen.events_until(self._gen_cursor):
                    while ing.backpressure:
                        await asyncio.sleep(0.001)
                    ing.submit(ev)
                    n += 1
                now = time.monotonic()
                if now - last_log >= log_every_s:
                    last_log = now
                    done = (self._gen_cursor - (end - timedelta(seconds=float(seconds)))).total_seconds()
                    log.info("generator warm-up: %.0f/%.0f s simulated, %d events, %.0f ev/s", done, seconds,
                             n, n / max(1e-9, now - t0))
                await asyncio.sleep(0)
            while len(ing) > 0:  # let the worker process everything in Warm_Up_Mode
                await asyncio.sleep(0.005)
            await asyncio.sleep(0)
        except BaseException:
            self.generator_warmup_state = "failed"
            raise
        finally:
            self.helix.warmup = False
        now = self.clock.now()
        if end > now and hasattr(self.clock, "advance"):
            self.clock.advance(end - now)  # stream time continues from the warm-up end
        dur = max(0.0, time.monotonic() - t0)
        rep = build_report(self.helix, n, dur, end)
        self.generator_warmup_report = rep
        self.generator_warmup_state = "done"
        log.info("generator warm-up done: %d events in %.1f s (%.0f ev/s), %d accounts, %d ready (>= %d events)",
                 rep.events, rep.duration_s, rep.events_per_s, rep.accounts, rep.accounts_ready, rep.min_history)
        return rep

    async def _warmup_then_start(self) -> None:
        try:
            await self.run_generator_warmup(self.generator_warmup_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - go live anyway without (complete) warm-up history
            log.exception("generator warm-up failed; starting live generation")
        if self.dataset is None and self.generator is not None:
            self.start_generator()
        if self.attacker_autostart:
            self._autostart_attacker()

    def generator_warmup_status(self) -> dict[str, Any]:
        rep = self.generator_warmup_report
        return {"seconds": float(self.generator_warmup_s), "state": self.generator_warmup_state,
                "report": None if rep is None else asdict(rep)}

    # --- simulated investigator (task 31.1, demo only) -------------------------------------

    def enable_sim_investigator(self, cfg: Any = None) -> Any:
        """Build the simulated investigator (idempotent). It reads the ground truth, so it
        needs the label reader; it labels through ``Helix.open_case`` / ``Helix.label``."""
        if self.sim_investigator is not None:
            return self.sim_investigator
        if self.gt_reader is None:
            raise LookupError("the simulated investigator needs the ground-truth reader")
        from helix.evaluation import SimInvestigator

        self.sim_investigator = SimInvestigator(self.helix, self.gt_reader, cfg)
        return self.sim_investigator

    def sim_investigator_status(self) -> dict[str, Any]:
        from helix.evaluation import sim_investigator_status_doc

        si = self.sim_investigator
        return sim_investigator_status_doc() if si is None else si.status(enabled=True)

    def sim_investigator_step(self, now: Optional[datetime] = None) -> list[Any]:
        si = self.sim_investigator
        return [] if si is None else si.step(now)

    async def _sim_investigator_loop(self) -> None:
        while True:
            await asyncio.sleep(float(self.sim_investigator.cfg.interval_s))
            try:
                self.sim_investigator_step()
            except Exception:  # noqa: BLE001 - one bad tick must not stop the loop
                log.exception("simulated investigator tick failed")


def attach_learner(helix: Helix) -> Any:
    """Attach the Feedback_Learner if ``helix.learn`` is available; None otherwise
    (weight rollback then answers 501)."""
    try:
        from helix.learn import attach
    except Exception:  # noqa: BLE001 - optional component
        log.warning("Feedback_Learner unavailable; weight rollback is disabled", exc_info=True)
        return None
    try:
        return attach(helix)
    except Exception:  # noqa: BLE001
        log.exception("Feedback_Learner attach failed; weight rollback is disabled")
        return None


def _as_dt(v: Any) -> Optional[datetime]:
    """Stored timestamp (tagged-JSON datetime or ISO string) -> aware datetime."""
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _apply_restored_state(helix: Helix, restored: Any, clock: SimClock, storage: Any = None) -> None:
    """Apply a ``RestoredState`` to a freshly built Helix, before the first new event
    (Req 24.6, 24.7): stream time, Behavioural_DNA / CPD snapshot, ``seq_no``, the
    Temporal_Graph (rebuilt from stored events), Cases with their Active_Queue / Backlog
    membership and history, and Evidence_Chains. Sensor_Weights are restored by the
    Feedback_Learner (``restore_live``) in ``AppState.build``."""
    from helix.changepoint import profile_store_rebaser
    from helix.storage import profile_store_from_dict

    # Stream time first: restored Cases and the graph are stamped at or before it, and the
    # new budget periods count from the restart.
    latest = restored.latest_event_ts
    if latest is not None:
        now = clock.now()
        if latest > now and hasattr(clock, "advance"):
            clock.advance(latest - now)
        helix.prioritiser.start = clock.now()

    seq = restored.last_seq_no or 0
    snap = restored.snapshot
    if snap is not None:
        log.info("restoring DNA/CPD from snapshot %d at %s", snap.snapshot_no, snap.ts)
        profiles = profile_store_from_dict(snap.dna)
        helix.profiles = helix.stage1.profiles = profiles
        helix.cpd.load_dict(snap.cpd)
        helix.cpd.on_regime_shift = profile_store_rebaser(profiles)
        snap_seq = snap.dna.get("last_seq_no") if isinstance(snap.dna, Mapping) else None
        if isinstance(snap_seq, int):
            seq = max(seq, snap_seq)
    helix.seq_no = seq

    if storage is not None and latest is not None:
        helix.graph = storage.rebuild_graph(helix.cfg.graph.window_s, latest)

    _restore_cases(helix, restored, storage)

    from helix.explain import loads

    for row in restored.evidence_chains:
        try:
            helix.chains[row["case_id"]] = loads(row["chain"])
        except Exception:  # noqa: BLE001 - one bad chain must not stop the restart
            log.exception("failed to restore the Evidence_Chain of %s", row.get("case_id"))
    log.info("restored seq_no=%d, %d cases, %d evidence chains", helix.seq_no,
             len(helix.cases.cases), len(helix.chains))


def _restore_cases(helix: Helix, restored: Any, storage: Any) -> None:
    """Rebuild Case_Manager and Prioritiser state from the stored Case rows (Req 24.6)."""
    from collections import defaultdict

    from helix.cases import Case, CaseHistoryEntry, CaseKey, CaseStatus, ClosureReason, Label, seq_of
    from helix.cases.prioritiser import _Entry
    from helix.config import KillChainStage, SeverityTier

    rows = sorted(restored.cases, key=lambda r: seq_of(r["case_id"]))
    if not rows:
        return
    history: dict[str, list[CaseHistoryEntry]] = defaultdict(list)
    for h in restored.case_history:  # ordered by (case_id, entry_no)
        history[h["case_id"]].append(CaseHistoryEntry(
            h["case_id"], _as_dt(h["ts"]), h["action"], h.get("prev"), h.get("new"), h.get("actor") or "system"))

    # Member events and stage-1/2 assessments: amounts and best assessment for the next
    # recompute, plus the pinned per-tx info used for amount-at-risk and labels.
    members = {tx for r in rows for tx in r.get("tx_ids", ())}
    events: dict[str, Any] = {}
    s1: dict[str, Any] = {}
    s2: dict[str, Any] = {}
    if storage is not None and members:
        events = {se.event.tx_id: se for se in storage.events() if se.event.tx_id in members}
        for sa in storage.assessments():
            fa = sa.assessment
            if fa.tx_id in members:
                (s1 if fa.stage == 1 else s2)[fa.tx_id] = fa
    _pin_members(helix, events, s1, s2)

    cm, pr = helix.cases, helix.prioritiser
    high = (SeverityTier.HIGH, SeverityTier.CRITICAL)
    for n, r in enumerate(rows, start=1):
        cid = r["case_id"]
        seq = seq_of(cid)
        hist = history.get(cid, [])
        last = {h.action: h for h in hist}  # latest entry per action
        status = CaseStatus(r["status"])
        tier = SeverityTier(r["tier"])
        placement = Placement(r["placement"]) if status is not CaseStatus.CLOSED else Placement.NONE
        tx_ids = set(r.get("tx_ids", ()))
        created = _as_dt(r.get("created_at")) or _as_dt(r["updated_at"])
        esc, lab, opened = last.get("escalate"), last.get("label"), last.get("open")
        kcs = r.get("kill_chain_stage")
        elb = r.get("expected_loss_base")
        # Closure_Reason (Req 41.1): stored on rows written since task 22.2; older CLOSED rows
        # infer it from the label / merge / auto_clear history.
        reason, closed_at = r.get("closure_reason"), _as_dt(r.get("closed_at"))
        if status is CaseStatus.CLOSED and reason is None:
            reason = ("LABELLED" if r.get("label") is not None else "MERGED" if r.get("merged_into")
                      else "AUTO_CLEARED" if "auto_clear" in last else None)
        if status is CaseStatus.CLOSED and closed_at is None:
            end = last.get({"LABELLED": "label", "MERGED": "merge", "AUTO_CLEARED": "auto_clear"}.get(reason or ""))
            closed_at = None if end is None else end.ts
        case = Case(
            case_id=cid, seq_no=seq, status=status, tier=tier, risk=float(r["risk"]),
            risk_at_creation=float(r["risk"]), expected_loss=Decimal(r["expected_loss"]),
            priority=Decimal(r["priority"]), accounts=set(r.get("accounts", ())), tx_ids=tx_ids,
            assignee=r.get("assignee"), created_at=created, updated_at=_as_dt(r["updated_at"]),
            admitted_period=None, placement=placement, merged_into=r.get("merged_into"),
            label=None if r.get("label") is None else Label(r["label"]), label_note=None,
            escalated=esc is not None, history=list(hist),
            assessments={tx: s1[tx] for tx in sorted(tx_ids) if tx in s1},
            amounts={tx: events[tx].event.amount for tx in sorted(tx_ids) if tx in events},
            opened_at=None if opened is None else opened.ts,
            label_by=None if lab is None else lab.actor, label_at=None if lab is None else lab.ts,
            escalated_at=None if esc is None else esc.ts, escalated_by=None if esc is None else esc.actor,
            withheld=tier in high,  # its events were already withheld before the restart
            update_no=n,
            kill_chain_stage=None if kcs is None else KillChainStage(kcs),
            expected_loss_base=None if elb is None else Decimal(elb),
            closure_reason=None if reason is None else ClosureReason(reason),
            closed_at=closed_at,
        )
        cm.cases[cid] = case
        helix._history_saved[cid] = len(hist)  # history rows already stored: append only new ones
        if not case.is_open:
            continue
        for tx in tx_ids:
            cm.tx_to_case[tx] = cid
        for a in case.accounts:
            cm.account_to_cases.setdefault(a, set()).add(cid)
        if placement in (Placement.ACTIVE, Placement.BACKLOG):
            # Re-insert with the stored membership (no re-admission against the new budget).
            key = CaseKey(cid, seq, created, case.expected_loss, opened=status is CaseStatus.IN_REVIEW)
            prio = pr.priority_for(case.expected_loss)
            sort = (-prio, created, seq, cid)
            pr._entries[cid] = _Entry(key, prio, placement, None, sort)
            (pr.active if placement is Placement.ACTIVE else pr.backlog).add(sort)
            case.priority = prio
    cm._next_seq = max(cm._next_seq, max(seq_of(r["case_id"]) for r in rows) + 1)
    cm._touch = max(cm._touch, len(rows))


def _pin_members(helix: Helix, events: Mapping[str, Any], s1: Mapping[str, Any], s2: Mapping[str, Any]) -> None:
    """Pin restored Case members' per-tx info (event + assessments) in the pipeline, as it
    is for live Case members, so summaries and labels see them after a restart."""
    try:
        from helix.core.pipeline import _TxInfo
        from helix.ingest.ingestor import Lateness, LatenessKind
    except ImportError:  # pragma: no cover - pipeline internals changed
        return
    pinned = getattr(helix, "_pinned", None)
    if pinned is None:
        return
    for tx, se in events.items():
        a1 = s1.get(tx)
        if a1 is None:
            continue
        lat = Lateness(LatenessKind.LATE, se.lateness_s) if se.late else Lateness(LatenessKind.IN_ORDER)
        try:
            pinned[tx] = _TxInfo(se.event, a1, s2.get(tx, a1), (), lat, se.received_at or se.event.ts,
                                 frozenset(), None)
        except TypeError:  # pragma: no cover - _TxInfo signature changed
            return


__all__ = ["LATENCY_TARGET_MS", "AppState", "attach_learner"]
