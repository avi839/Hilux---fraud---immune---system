"""Unit tests for helix.attacker (Attacker_Agent, Pattern_Genome, campaign generators)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest

from helix.attacker import (
    ALL_TYPES,
    BOUNDS,
    AttackerAgent,
    CampaignContext,
    PatternType,
    TargetPool,
    attach_attacker,
    default_genome,
    generate,
)
from helix.config import HelixConfig
from helix.config.schema import AttackerConfig, GeneratorConfig
from helix.core import Helix
from helix.ingest import StreamIngestor
from helix.motifs import MotifType
from helix.sim import InMemoryGroundTruth, ManualClock, SyntheticGenerator

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


class Recorder:
    """Minimal ingest: records submits; optional backpressure flag."""

    def __init__(self) -> None:
        self.events = []
        self.backpressure = False

    def submit(self, ev):
        self.events.append(ev)


def pool(n: int = 60) -> list[str]:
    gen = SyntheticGenerator(GeneratorConfig(accounts=n), ManualClock(T0), InMemoryGroundTruth(), seed=1)
    return [p.account_id for p in gen.profiles]


def ctx(seed: int = 0, **kw) -> CampaignContext:
    base = dict(rng=np.random.default_rng(seed), start=T0, targets=TargetPool(pool()), id_prefix=f"t{seed}",
                reporting_threshold=Decimal("10000"), max_events=200)
    base.update(kw)
    return CampaignContext(**base)


def agent(cfg: AttackerConfig | None = None, **kw) -> tuple[AttackerAgent, ManualClock, InMemoryGroundTruth, Recorder]:
    clock, gt, ing = ManualClock(T0), InMemoryGroundTruth(), Recorder()
    cfg = cfg or AttackerConfig(enabled=True, attack_fraction=1.0)
    a = AttackerAgent(cfg, clock, gt, ing, seed=kw.pop("seed", 7), account_pool=pool(), **kw)
    return a, clock, gt, ing


def run(a: AttackerAgent, clock: ManualClock, seconds: int, step: int = 60, legit: int = 1000) -> None:
    for _ in range(seconds // step):
        clock.advance(timedelta(seconds=step))
        a.note_ingested(legit, clock.now())
        a.tick(clock.now())


# ------------------------------------------------------------------ genomes


def test_initial_population_covers_all_seven_types_within_bounds():
    a, *_ = agent()
    gs = a.genomes()
    assert sorted(g.ptype.value for g in gs) == sorted(t.value for t in ALL_TYPES)
    assert len(ALL_TYPES) == 7 and PatternType.ROUND_TRIP is PatternType.ROUND_TRIP_CYCLE
    for g in gs:
        assert g.parent_id is None and g.generation == 0
        assert all(BOUNDS[g.ptype][k].contains(v) for k, v in g.params.items())


def test_genome_rejects_out_of_bounds_params():
    g = default_genome(PatternType.FAN_OUT, "G1")
    bad = dict(g.params, n_counterparties=500)
    with pytest.raises(ValueError):
        type(g)("G2", g.ptype, bad, None)


# ------------------------------------------------------- defaults satisfy motifs


@pytest.mark.parametrize(
    "ptype,motif",
    [
        (PatternType.FAN_OUT, MotifType.FAN_OUT),
        (PatternType.STRUCTURING, MotifType.STRUCTURING),
        (PatternType.FAN_IN, MotifType.FAN_IN),
        (PatternType.ROUND_TRIP_CYCLE, MotifType.ROUND_TRIP),
        (PatternType.MULE_CHAIN, MotifType.MULE_CHAIN),
    ],
)
def test_default_campaign_produces_matching_motif_in_real_helix(ptype, motif):
    events = generate(default_genome(ptype, "G1"), ctx(seed=3))
    helix = Helix(HelixConfig(), ManualClock(T0))
    found = set()
    for ev in events:
        out = helix.process(ev)
        found |= {m.motif_type for m in out.motifs if m.tx_ids <= {e.tx_id for e in events}}
    assert motif in found


def test_card_testing_is_small_tests_to_new_merchants_then_one_large():
    evs = generate(default_genome(PatternType.CARD_TESTING, "G1"), ctx())
    p = BOUNDS[PatternType.CARD_TESTING]
    assert len(evs) == p["n_tests"].default + 1
    assert len({e.src for e in evs}) == 1
    small, big = evs[:-1], evs[-1]
    assert all(e.amount <= Decimal("3.00") for e in small) and big.amount >= Decimal("2000")
    assert all(e.dst.startswith("MER") for e in evs)
    assert big.dst not in {e.dst for e in small}


def test_account_takeover_is_transactions_from_one_pool_victim_on_a_new_device():
    evs = generate(default_genome(PatternType.ACCOUNT_TAKEOVER, "G1"), ctx())
    victims = {e.src for e in evs}
    assert len(victims) == 1 and victims <= set(pool())
    devices = {e.device_id for e in evs} - {None}
    gen = SyntheticGenerator(GeneratorConfig(accounts=60), ManualClock(T0), InMemoryGroundTruth(), seed=1)
    known_devices = {d for p in gen.profiles for d in p.devices}
    assert len(devices) == 1 and devices.isdisjoint(known_devices)  # never the victim's device


def test_accounts_come_from_pool_or_fresh_ids_only():
    known = set(pool())
    for t in ALL_TYPES:
        for e in generate(default_genome(t, "G1"), ctx(seed=5)):
            for acct in (e.src, e.dst):
                assert acct in known or acct.startswith(("EXT", "MER"))
            assert e.src != e.dst


# --------------------------------------------------------------------- agent


def test_launches_one_campaign_per_interval_of_stream_time():
    a, clock, *_ = agent(AttackerConfig(enabled=True, launch_interval_s=300, attack_fraction=1.0))
    run(a, clock, 3600)
    assert len(a.campaigns()) == 13  # t0, t0+5min, ..., t0+60min
    starts = [c.started_at for c in a.campaigns()]
    assert starts == [T0 + timedelta(seconds=300 * i) for i in range(13)]


def test_disabled_agent_launches_nothing():
    a, clock, *_ = agent(AttackerConfig(enabled=False))
    run(a, clock, 3600)
    assert a.campaigns() == []


def test_label_write_failure_withholds_and_logs(caplog):
    class Flaky(InMemoryGroundTruth):
        def write(self, rec):
            if rec.event_id.endswith(("0", "1", "2", "3", "4")):
                raise OSError("disk full")
            super().write(rec)

    clock, ing = ManualClock(T0), Recorder()
    gt = Flaky()
    a = AttackerAgent(AttackerConfig(enabled=True, attack_fraction=1.0), clock, gt, ing, seed=1, account_pool=pool())
    with caplog.at_level(logging.ERROR, logger="helix.attacker.agent"):
        run(a, clock, 3 * 3600)
    sent = {e.tx_id for e in ing.events}
    labelled = {r.event_id for r in gt.records}
    assert sent == labelled and sent
    failed = {tx for c in a.campaigns() for tx in c.withheld_ids}
    assert failed and not (failed & sent)
    assert any(tx in caplog.text for tx in failed)
    rec = gt.records[0]
    assert rec.label == "FRAUD" and rec.campaign_id and rec.genome_id and rec.source.value == "ATTACKER"


def test_evasion_scored_only_after_detection_window_and_children_evolve():
    detected: set[str] = set()
    cfg = AttackerConfig(enabled=True, launch_interval_s=86_400, detection_window_s=3_600,
                         attack_fraction=1.0, top_k=2, children_per_parent=3)
    a2, clock2, _, _ = agent(cfg, is_detected=lambda tx: tx in detected, seed=11)
    a2.tick(T0)
    first = a2.campaigns()[0]
    assert first.campaign_id == "C000001"
    # Inject everything (default campaigns last < 1 day), then detect half and let the window pass.
    for _ in range(24 * 60):
        clock2.advance(timedelta(minutes=1))
        a2.note_ingested(10_000, clock2.now())
        a2.tick(clock2.now())
        cur = next(x for x in a2.campaigns() if x.campaign_id == first.campaign_id)
        if cur.injected_ids and len(cur.injected_ids) == len(cur.event_ids):
            break
    cur = next(x for x in a2.campaigns() if x.campaign_id == first.campaign_id)
    assert not cur.completed and cur.window_end is not None
    detected.update(cur.injected_ids[: len(cur.injected_ids) // 2])
    clock2.set(cur.window_end - timedelta(seconds=1))
    a2.tick(clock2.now())
    assert not next(x for x in a2.campaigns() if x.campaign_id == first.campaign_id).completed
    clock2.set(cur.window_end)
    a2.tick(clock2.now())
    done = next(x for x in a2.campaigns() if x.campaign_id == first.campaign_id)
    n = len(done.injected_ids)
    assert done.completed and done.evasion == pytest.approx(1 - (n // 2) / n)
    assert a2.genome(done.genome_id).evasion == pytest.approx(done.evasion)
    kids = [g for g in a2.genomes() if g.parent_id == done.genome_id]
    assert len(kids) == 3 and all(g.generation == 1 for g in kids)
    assert a2.lineage(kids[0].genome_id) == (done.genome_id, kids[0].genome_id)


def test_backpressure_pauses_injection():
    a, clock, _, ing = agent()
    ing.backpressure = True
    run(a, clock, 3600)
    assert ing.events == [] and a.pending_events > 0
    ing.backpressure = False
    run(a, clock, 60)
    assert ing.events


def test_zero_attack_fraction_injects_nothing():
    a, clock, _, ing = agent(AttackerConfig(enabled=True, attack_fraction=0.0))
    run(a, clock, 3600)
    assert ing.events == []


# --------------------------------------------------------------- integration


def test_attach_attacker_wires_pool_monitor_and_counts():
    cfg = HelixConfig(attacker=AttackerConfig(enabled=True, launch_interval_s=60, detection_window_s=60,
                                              attack_fraction=0.5))
    clock = ManualClock(T0)
    ingestor = StreamIngestor(cfg.ingest, clock)
    gen = SyntheticGenerator(GeneratorConfig(accounts=40, rate_eps=2), clock, InMemoryGroundTruth(), seed=2)
    completed: list[str] = []
    monitor = SimpleNamespace(detected_events=lambda: frozenset(), mark_campaign_completed=completed.append)
    gt = InMemoryGroundTruth()
    state = SimpleNamespace(cfg=cfg, clock=clock, ingestor=ingestor, generator=gen, monitor=monitor, attacker=None)
    runner = attach_attacker(state, writer=gt, seed=5)
    assert state.attacker is runner
    assert set(runner.agent.targets.accounts) == {p.account_id for p in gen.profiles}
    for _ in range(240):
        clock.advance(timedelta(seconds=30))
        for ev in gen.events_until(clock.now()):
            ingestor.submit(ev)
        runner.step()
        ingestor.drain()  # stand-in for the pipeline worker; otherwise backpressure pauses the attacker
    assert runner.agent.injected_total > 0
    assert len(gt.records) >= runner.agent.injected_total
    assert completed  # campaign completions reported to the monitor
    assert runner.campaigns()  # pass-through to the agent
