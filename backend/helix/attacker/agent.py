"""Attacker_Agent: seeded red team that co-evolves Pattern_Genomes against HELIX (design §17).

Driven by the caller: ``tick(now)`` launches campaigns on the stream-time schedule, injects
every scheduled event whose ``ts <= now`` (subject to the rate cap and backpressure),
completes campaigns whose detection window has passed and evolves the population.

Isolation: detection outcomes come in only through the injected ``is_detected(tx_id)``
callback (the API layer wires it to the Evaluation_Monitor's Detected_Events). The agent
writes labels through a ``GroundTruthWriter`` but never reads them, and it is given no
Canary information.

Rate cap (Req 20.9): within each stream-hour (bucketed on the tick time) an event is
injected only while ``injected + 1 <= frac * (legit + injected + 1)``, where ``legit`` is
what the caller reported through ``note_ingested``. Both counts only grow within an hour,
so the hourly ratio holds at hour end. Deferred events keep their scheduled ``ts``; they are
injected in heap order ``(ts, seq)``, so the injected stream stays non-decreasing in ``ts``.

Cyber fusion (``soc`` with ``enabled``, Req 28.6): account-takeover campaigns carry an
Auth_Event leg (credential stuffing, SIM swap or device takeover). Auth_Events go through the
same label-first injection (a FRAUD Ground_Truth_Store record keyed by ``event_id``, then
``ingest.submit``) and count against the rate cap. Evasion is scored over the campaign's
injected Transaction_Events only: Cases contain transactions, and Auth_Events are judged
through the transactions they are linked to.
"""

from __future__ import annotations

import heapq
import itertools
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

import numpy as np

from helix.codec import AuthEvent, event_id_of
from helix.config.schema import AttackerConfig, SocConfig
from helix.sim.clock import SimClock
from helix.sim.ground_truth import GroundTruthRecord, GroundTruthWriter, GTSource

from .campaigns import CampaignContext, TargetPool, generate
from .genome import ALL_TYPES, PatternGenome, default_genome, mutate, with_score

log = logging.getLogger(__name__)

IsDetected = Callable[[str], bool]
_HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class AttackCampaign:
    campaign_id: str
    genome_id: str
    started_at: datetime
    event_ids: tuple[str, ...]  # planned events, sorted by ts; 1..max_events
    window_end: Optional[datetime] = None  # last injection time + detection window
    evasion: Optional[float] = None  # 1 - |detected ∩ injected| / |injected|
    injected_ids: tuple[str, ...] = ()
    withheld_ids: tuple[str, ...] = ()  # label write failed (Req 20.11) or not accepted
    completed: bool = False


@dataclass
class _Live:
    campaign: AttackCampaign
    parent_id: Optional[str]
    pending: int
    injected: list[str] = field(default_factory=list)
    withheld: list[str] = field(default_factory=list)
    last_injected_at: Optional[datetime] = None


def _hour_of(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


class AttackerAgent:
    def __init__(
        self,
        cfg: AttackerConfig,
        clock: SimClock,
        writer: GroundTruthWriter,
        ingest: Any,  # StreamIngestor or anything with submit(ev) (and optionally .backpressure)
        seed: int = 0,
        account_pool: Sequence[str] = (),
        is_detected: Optional[IsDetected] = None,
        *,
        reporting_threshold: Decimal | float | str = Decimal("10000"),
        start: Optional[datetime] = None,
        on_campaign_complete: Optional[Callable[[AttackCampaign], None]] = None,
        soc: Optional[SocConfig] = None,
    ) -> None:
        self.auth_enabled = bool(soc is not None and soc.enabled)
        self._auth_ids: set[str] = set()  # injected/planned Auth_Event ids (not scored)
        self.cfg = cfg
        self.clock = clock
        self.writer = writer
        self.ingest = ingest
        self.seed = int(seed)
        self.rng = np.random.default_rng(self.seed)
        self.targets = TargetPool(account_pool)
        self.is_detected: IsDetected = is_detected if is_detected is not None else (lambda _tx: False)
        self.reporting_threshold = Decimal(str(reporting_threshold))
        self.on_campaign_complete = on_campaign_complete

        self._genomes: dict[str, PatternGenome] = {}
        self._prior: dict[str, float] = {}  # retirement prior for unscored genomes
        self._launched: set[str] = set()
        self._gid = itertools.count(1)
        self._cid = itertools.count(1)
        self._seq = itertools.count()
        self._heap: list[tuple[datetime, int, str, Any]] = []  # Transaction_ or Auth_Events
        self._live: dict[str, _Live] = {}
        self._done: dict[str, AttackCampaign] = {}

        self._next_launch: datetime = start if start is not None else clock.now()
        self._hour: Optional[datetime] = None
        self._legit_h = 0
        self._inj_h = 0
        self.injected_total = 0  # Transaction_ and Auth_Events
        self.injected_tx_total = 0  # Transaction_Events only (matches ``ingest.counters.accepted``)
        self.withheld_total = 0
        self.deferred = 0  # rate-cap or backpressure deferrals (event-ticks)
        self.history: list[tuple[datetime, int, int]] = []  # (hour, legit, injected) closed hours

        for ptype in ALL_TYPES:  # Req 20.1: one genome per type
            g = default_genome(ptype, self._new_gid())
            self._genomes[g.genome_id] = g
            self._prior[g.genome_id] = 1.0  # optimistic: untested genomes are kept

    # ----------------------------------------------------------------- queries

    def genomes(self) -> list[PatternGenome]:
        """All genomes, retired ones included (records are kept, Req 20.15)."""
        return list(self._genomes.values())

    def active_genomes(self) -> list[PatternGenome]:
        return [g for g in self._genomes.values() if not g.retired]

    def genome_techniques(self, genome_id: str) -> tuple[str, ...]:
        """HELIX_Technique tags of a Pattern_Genome (Req 30.5): one or more catalogue ids from
        its type; ``()`` for an unknown genome or unmapped type."""
        from helix.soc.techniques import genome_techniques  # local: SOC is optional

        g = self._genomes.get(genome_id)
        if g is None:
            return ()
        try:
            return genome_techniques(g.ptype)
        except ValueError:
            return ()

    def genome(self, genome_id: str) -> PatternGenome:
        return self._genomes[genome_id]

    def campaigns(self) -> list[AttackCampaign]:
        live = [self._snapshot(lv) for lv in self._live.values()]
        return sorted([*self._done.values(), *live], key=lambda c: c.campaign_id)

    def completed_campaigns(self) -> list[AttackCampaign]:
        return sorted(self._done.values(), key=lambda c: c.campaign_id)

    def lineage(self, genome_id: Optional[str] = None) -> Any:
        """Root-first chain of genome ids. With no argument: ``{genome_id: chain}`` for all."""
        if genome_id is None:
            return {gid: self.lineage(gid) for gid in self._genomes}
        chain = []
        cur: Optional[str] = genome_id
        while cur is not None:
            chain.append(cur)
            cur = self._genomes[cur].parent_id
        return tuple(reversed(chain))

    @property
    def pending_events(self) -> int:
        return len(self._heap)

    # ------------------------------------------------------------------ inputs

    def note_ingested(self, n: int, at: Optional[datetime] = None) -> None:
        """Report ``n`` legitimate events ingested at stream time ``at`` (default: now).

        Reports for an hour earlier than the current one are ignored (conservative)."""
        if n <= 0:
            return
        t = at if at is not None else self.clock.now()
        self._roll(t)
        if _hour_of(t) == self._hour:
            self._legit_h += int(n)

    def tick(self, now: Optional[datetime] = None) -> None:
        now = now if now is not None else self.clock.now()
        self._roll(now)
        if self.cfg.enabled:
            while self._next_launch <= now:
                self._launch(self._next_launch)
                self._next_launch += timedelta(seconds=float(self.cfg.launch_interval_s))
        self._inject_due(now)
        self._complete_due(now)

    # ------------------------------------------------------------------- launch

    def _new_gid(self) -> str:
        return f"G{next(self._gid):06d}"

    def _pick_genome(self) -> PatternGenome:
        active = self.active_genomes()
        fresh = [g for g in active if g.genome_id not in self._launched]
        if fresh:
            return min(fresh, key=lambda g: g.genome_id)  # FIFO over untested genomes
        w = np.array([(g.evasion if g.evasion is not None else 0.5) + 0.05 for g in active])
        return active[int(self.rng.choice(len(active), p=w / w.sum()))]

    def _launch(self, at: datetime) -> AttackCampaign:
        g = self._pick_genome()
        cid = f"C{next(self._cid):06d}"
        ctx = CampaignContext(
            rng=self.rng, start=at, targets=self.targets, id_prefix=f"{self.seed}:{cid}",
            reporting_threshold=self.reporting_threshold, max_events=int(self.cfg.max_events),
            auth=self.auth_enabled,
        )
        events = generate(g, ctx)
        self._auth_ids.update(e.event_id for e in events if isinstance(e, AuthEvent))
        camp = AttackCampaign(cid, g.genome_id, at, tuple(event_id_of(e) for e in events))
        self._launched.add(g.genome_id)
        self._live[cid] = _Live(camp, g.parent_id, pending=len(events))
        for ev in events:
            heapq.heappush(self._heap, (ev.ts, next(self._seq), cid, ev))
        log.info("attacker: launched %s from %s (%s, %d events)", cid, g.genome_id, g.ptype.value, len(events))
        return camp

    # ------------------------------------------------------------------- inject

    def _roll(self, t: datetime) -> None:
        h = _hour_of(t)
        if self._hour is None or h > self._hour:
            if self._hour is not None:
                self.history.append((self._hour, self._legit_h, self._inj_h))
            self._hour, self._legit_h, self._inj_h = h, 0, 0

    def _rate_allows(self) -> bool:
        return self._inj_h + 1 <= float(self.cfg.attack_fraction) * (self._legit_h + self._inj_h + 1)

    def _inject_due(self, now: datetime) -> None:
        heap = self._heap
        while heap and heap[0][0] <= now:
            if getattr(self.ingest, "backpressure", False) or not self._rate_allows():
                self.deferred += 1
                return  # keep order: nothing later may jump ahead of the deferred head
            _, _, cid, ev = heapq.heappop(heap)
            live = self._live[cid]
            live.pending -= 1
            eid = event_id_of(ev)  # tx_id, or event_id for an Auth_Event
            rec = GroundTruthRecord(
                event_id=eid, label="FRAUD", source=GTSource.ATTACKER, written_at=self.clock.now(),
                campaign_id=cid, genome_id=live.campaign.genome_id, parent_genome_id=live.parent_id,
                technique_tags=self.genome_techniques(live.campaign.genome_id) if self.auth_enabled else (),
            )
            try:
                self.writer.write(rec)  # label first (Req 20.10)
            except Exception:  # noqa: BLE001 - Req 20.11: withhold, log, continue
                live.withheld.append(eid)
                self.withheld_total += 1
                log.exception("attacker: label write failed for event %s; event withheld", eid)
                continue
            res = self.ingest.submit(ev)
            if not getattr(res, "accepted", True):
                live.withheld.append(eid)
                self.withheld_total += 1
                log.warning("attacker: event %s not accepted by the ingestor (%s)", eid,
                            getattr(res, "status", res))
                continue
            live.injected.append(eid)
            live.last_injected_at = now
            self._inj_h += 1
            self.injected_total += 1
            if not isinstance(ev, AuthEvent):
                self.injected_tx_total += 1

    # ----------------------------------------------------------------- complete

    def _snapshot(self, lv: _Live) -> AttackCampaign:
        end = None
        if lv.last_injected_at is not None:
            end = lv.last_injected_at + timedelta(seconds=float(self.cfg.detection_window_s))
        return replace(lv.campaign, window_end=end, injected_ids=tuple(lv.injected), withheld_ids=tuple(lv.withheld))

    def _complete_due(self, now: datetime) -> None:
        for cid in sorted(self._live):
            lv = self._live[cid]
            if lv.pending:
                continue
            snap = self._snapshot(lv)
            if snap.window_end is not None and now < snap.window_end:
                continue
            self._complete(cid, snap)

    def _complete(self, cid: str, snap: AttackCampaign) -> None:
        del self._live[cid]
        evasion = None
        scored = [i for i in snap.injected_ids if i not in self._auth_ids]  # Transaction_Events only
        if scored:
            hit = sum(1 for tx in scored if self.is_detected(tx))
            evasion = 1.0 - hit / len(scored)
        self._auth_ids.difference_update(snap.event_ids)  # bounded: only live campaigns' ids
        done = replace(snap, evasion=evasion, completed=True)
        self._done[cid] = done
        if evasion is not None:
            g = self._genomes[done.genome_id]
            self._genomes[g.genome_id] = with_score(g, evasion)
            self._evolve()
        log.info("attacker: completed %s (evasion=%s)", cid, evasion)
        if self.on_campaign_complete is not None:
            try:
                self.on_campaign_complete(done)
            except Exception:  # noqa: BLE001 - a reporting hook must not stop the attacker
                log.exception("attacker: campaign-complete hook failed for %s", cid)

    # ------------------------------------------------------------------- evolve

    def select_parents(self) -> list[PatternGenome]:
        """Top K active, scored genomes by (-evasion, genome_id) (Req 20.6)."""
        scored = [g for g in self.active_genomes() if g.evasion is not None]
        scored.sort(key=lambda g: (-g.evasion, g.genome_id))
        return scored[: int(self.cfg.top_k)]

    def _evolve(self) -> list[PatternGenome]:
        children = []
        for parent in self.select_parents():
            for _ in range(int(self.cfg.children_per_parent)):
                child = mutate(parent, self.rng, self._new_gid())
                self._genomes[child.genome_id] = child
                self._prior[child.genome_id] = float(parent.evasion)
                children.append(child)
        self._enforce_cap()
        return children

    def retire_key(self, g: PatternGenome) -> tuple[float, int]:
        """Retirement order (Req 20.15): lowest evasion first, newest first among equals.

        Unscored genomes use a prior (their parent's evasion; 1.0 for the initial set) so
        fresh children of strong parents are not retired before they are ever tested."""
        ev = g.evasion if g.evasion is not None else self._prior.get(g.genome_id, 1.0)
        return (ev, -int(g.genome_id[1:]))

    def _enforce_cap(self) -> None:
        active = self.active_genomes()
        excess = len(active) - int(self.cfg.active_genome_cap)
        if excess <= 0:
            return
        active.sort(key=self.retire_key)
        for g in active[:excess]:
            self._genomes[g.genome_id] = replace(g, retired=True)
            log.info("attacker: retired %s (evasion=%s)", g.genome_id, g.evasion)


__all__ = ["AttackCampaign", "AttackerAgent", "IsDetected"]
