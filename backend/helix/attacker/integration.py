"""Wire an AttackerAgent into an API ``AppState`` without importing the evaluation package.

``attach_attacker(state)`` builds the agent from ``state.cfg.attacker`` and duck-types the rest:

- account pool: ``state.generator.profiles[*].account_id``; with soc.enabled and
  ``state.canaries`` the Canary accounts / payees are mixed in (``CanaryRegistry.population``),
  as plain strings, so the agent cannot tell which ids are Canaries (Req 33.7);
- detection outcomes: ``state.monitor.detected_events()`` (snapshotted once per tick);
- campaign completion: ``state.monitor.mark_campaign_completed(campaign_id)`` if present;
- legit counts: ``state.ingestor.counters.accepted`` minus the agent's own accepted injections;
- labels: ``writer`` if given (pass the producer side of the evaluation package's label
  channel), else ``state.gt_writer`` when present, else an in-memory writer (labels then
  reach no reader; logged as a warning);
- stream time: the later of ``state.clock.now()`` and the generator feeder's cursor.

The returned ``AttackerRunner`` is stored on ``state.attacker``; ``start()`` spawns the
background tick task on the running loop, ``stop()`` cancels it, ``step()`` runs one tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import datetime
from typing import Any, Optional

from helix.sim.ground_truth import GroundTruthWriter, InMemoryGroundTruth

from .agent import AttackCampaign, AttackerAgent

log = logging.getLogger(__name__)


class _DetectedView:
    """``is_detected`` callback over a per-tick snapshot of ``monitor.detected_events()``."""

    def __init__(self, monitor: Any) -> None:
        self.monitor = monitor
        self._snap: frozenset[str] = frozenset()

    def refresh(self) -> None:
        fn = getattr(self.monitor, "detected_events", None)
        if fn is not None:
            self._snap = frozenset(fn())

    def __call__(self, tx_id: str) -> bool:
        return tx_id in self._snap


def _find_writer(state: Any) -> Optional[GroundTruthWriter]:
    w = getattr(state, "gt_writer", None)
    return w if w is not None and hasattr(w, "write") else None


class AttackerRunner:
    def __init__(self, state: Any, agent: AttackerAgent, detected: _DetectedView, tick_s: float) -> None:
        self.state = state
        self.agent = agent
        self.detected = detected
        self.tick_s = float(tick_s)
        self._legit_seen = 0
        self._task: Optional[asyncio.Task] = None

    def stream_now(self) -> datetime:
        return _stream_now(self.state)

    def step(self) -> None:
        now = self.stream_now()
        accepted = int(self.state.ingestor.counters.accepted)
        # Transaction_Event counters exclude Auth_Events, so subtract injected transactions only.
        legit = max(0, accepted - self.agent.injected_tx_total)
        if legit > self._legit_seen:
            self.agent.note_ingested(legit - self._legit_seen, at=now)
            self._legit_seen = legit
        self.detected.refresh()
        self.agent.tick(now)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if self.running:
            return False
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="helix-attacker")
        return True

    def stop(self) -> bool:
        if not self.running:
            return False
        assert self._task is not None
        self._task.cancel()
        return True

    async def aclose(self) -> None:
        t, self._task = self._task, None
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_s)
            try:
                self.step()
            except Exception:  # noqa: BLE001 - one bad tick must not stop the attacker
                log.exception("attacker tick failed")

    # Convenience pass-throughs (genomes(), campaigns(), lineage(), ...) for the API layer.
    def __getattr__(self, name: str) -> Any:
        if name == "agent":  # not yet set during construction
            raise AttributeError(name)
        return getattr(self.agent, name)


def _stream_now(state: Any) -> datetime:
    now = state.clock.now()
    cursor = getattr(state, "_gen_cursor", None)  # generator feeder's stream-time cursor
    return cursor if cursor is not None and cursor > now else now


def attach_attacker(
    state: Any,
    *,
    seed: Optional[int] = None,
    writer: Optional[GroundTruthWriter] = None,
    tick_s: float = 0.25,
    start: bool = False,
    force_enabled: bool = True,
) -> AttackerRunner:
    cfg = state.cfg.attacker
    if force_enabled and not cfg.enabled:
        cfg = cfg.model_copy(update={"enabled": True})
    gen = getattr(state, "generator", None)
    pool = [p.account_id for p in getattr(gen, "profiles", ())]
    if seed is None:
        seed = int(getattr(getattr(state.cfg, "generator", None), "default_seed", 0))
    canaries = getattr(state, "canaries", None)
    soc_cfg = getattr(state.cfg, "soc", None)
    if canaries is not None and soc_cfg is not None and soc_cfg.enabled:
        # Req 33.7: Canary accounts and payees are mixed into the target population as plain
        # strings (no Canary flag reaches the agent); TargetPool sorts, so order is irrelevant.
        pool = canaries.population(pool, random.Random(seed))
    monitor = getattr(state, "monitor", None)
    if monitor is None:
        log.warning("attacker: no Evaluation_Monitor attached; every campaign will score evasion 1.0")
    detected = _DetectedView(monitor)
    w = writer if writer is not None else _find_writer(state)
    if w is None:
        log.warning("attacker: no label writer found; using an in-memory writer")
        w = InMemoryGroundTruth()

    def on_complete(c: AttackCampaign) -> None:
        fn = getattr(monitor, "mark_campaign_completed", None)
        if fn is not None:
            fn(c.campaign_id)

    rt = getattr(getattr(state.cfg, "graph", None), "reporting_threshold", 10_000)
    agent = AttackerAgent(
        cfg, state.clock, w, state.ingestor, seed=seed, account_pool=pool, is_detected=detected,
        reporting_threshold=str(rt), start=_stream_now(state), on_campaign_complete=on_complete,
        soc=getattr(state.cfg, "soc", None),  # Auth_Event legs only when soc.enabled (Req 28.6)
    )
    runner = AttackerRunner(state, agent, detected, tick_s)
    state.attacker = runner
    if start:
        runner.start()
    return runner


__all__ = ["AttackerRunner", "attach_attacker"]
