"""ACT cost on the default generator config (task 19.1; Requirements 12, 25). Deselected by
default (``-m perf`` to run)."""

from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime, timedelta, timezone

import pytest

from helix.cases import CaseManager, Prioritiser
from helix.config import HelixConfig
from helix.config.schema import PrioritiserConfig, RiskConfig
from helix.core.pipeline import Helix, Stage
from helix.sim.clock import ManualClock
from helix.sim.generator import SyntheticGenerator
from helix.sim.ground_truth import InMemoryGroundTruth
from tests.unit.test_cases import T0, ev, fa

GEN_T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)


def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


@pytest.mark.perf
def test_case_update_cost_does_not_grow_with_case_size():
    """Adding an event to a Case of 5,000 events costs about what it costs at 100 (O(1)
    recompute), with the bound lifted so one Case can grow that large."""
    clock = ManualClock(T0)
    pr = Prioritiser(PrioritiserConfig(alert_budget=20, investigation_cost=0), clock)
    cm = CaseManager(RiskConfig(), pr, clock, max_accounts=100_000, max_events=1_000_000)

    def add_many(start: int, n: int) -> list[float]:
        out = []
        for i in range(start, start + n):
            clock.advance(timedelta(seconds=1))
            tx = f"t{i}"
            t = time.perf_counter()
            cm.on_scored_event(ev(tx, "A", f"B{i % 40}"), fa(tx, 0.7 + (i % 7) / 100), 0.75)
            out.append(time.perf_counter() - t)
        return out

    small = statistics.median(add_many(0, 100)[20:])
    add_many(100, 4_800)
    large = statistics.median(add_many(4_900, 100)[20:])
    assert len(cm.open_cases()) == 1 and len(cm.open_cases()[0].tx_ids) == 5_000
    print(f"\nCase update median: {small * 1e3:.3f} ms at 100 events, {large * 1e3:.3f} ms at 5,000")
    assert large <= 3 * small + 1e-4


@pytest.mark.perf
def test_default_generator_config_stays_responsive():
    """The default generator (1,000 accounts at 50 ev/s) for 3 simulated minutes: Cases stay
    within the bound and the pipeline keeps up with 50 ev/s with ACT well below it."""
    logging.getLogger("helix").setLevel(logging.ERROR)
    cfg = HelixConfig()
    h = Helix(cfg, ManualClock(GEN_T0))
    gen = SyntheticGenerator(cfg.generator, ManualClock(GEN_T0), InMemoryGroundTruth(),
                             seed=cfg.generator.default_seed)
    n = int(180 * cfg.generator.rate_eps)
    act: list[float] = []
    e2e: list[float] = []
    start = time.perf_counter()
    for _ in range(n):
        out = h.process(next(gen))
        act.append(out.latency_ms[Stage.ACT])
        e2e.append(out.latency_ms[Stage.END_TO_END])
    wall = time.perf_counter() - start
    tail_act, tail_e2e = act[-1_000:], e2e[-1_000:]
    open_cases = h.cases.open_cases()
    biggest = max(len(c.accounts) for c in open_cases)
    print(f"\n{n} events in {wall:.1f}s ({n / wall:.0f} ev/s); last 1,000: ACT p50={pct(tail_act, .5):.2f} "
          f"p95={pct(tail_act, .95):.2f} max={max(tail_act):.2f} ms, end-to-end p50={pct(tail_e2e, .5):.2f} "
          f"p95={pct(tail_e2e, .95):.2f} ms; open Cases={len(open_cases)}, largest={biggest} accounts, "
          f"overflows={h.cases.overflows}, motifs dropped={h.cases.motifs_dropped}")
    assert biggest <= cfg.risk.case_max_accounts
    assert pct(tail_act, 0.5) < 5.0 and pct(tail_act, 0.95) < 25.0
    assert statistics.fmean(tail_e2e) < 1_000 / cfg.generator.rate_eps  # keeps up with the generator
