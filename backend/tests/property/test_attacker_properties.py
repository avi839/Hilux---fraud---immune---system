"""Property tests for helix.attacker (Properties 61-65, Requirement 20)."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.attacker import (
    ALL_TYPES,
    BOUNDS,
    AttackerAgent,
    CampaignContext,
    PatternGenome,
    TargetPool,
    generate,
    mutate,
)
from helix.codec import OPTIONAL_FIELDS, REQUIRED_FIELDS, format
from helix.config.schema import AttackerConfig
from helix.sim import InMemoryGroundTruth, ManualClock

pytestmark = pytest.mark.property

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
POOL = [f"ACC{i:06d}" for i in range(80)]
SCHEMA_KEYS = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS)


# ---------------------------------------------------------------- strategies


@st.composite
def genomes(draw, gid: str = "G000001") -> PatternGenome:
    ptype = draw(st.sampled_from(ALL_TYPES))
    params = {}
    for k, s in BOUNDS[ptype].items():
        if s.kind == "int":
            params[k] = draw(st.integers(int(s.lo), int(s.hi)))
        else:
            params[k] = draw(st.floats(s.lo, s.hi, allow_nan=False))
    return PatternGenome(gid, ptype, params, None, draw(st.integers(0, 5)))


configs = st.builds(
    AttackerConfig,
    enabled=st.just(True),
    launch_interval_s=st.sampled_from([300.0, 600.0, 1_800.0]),
    max_events=st.integers(1, 60),
    detection_window_s=st.sampled_from([60.0, 1_800.0, 7_200.0]),
    top_k=st.integers(1, 4),
    children_per_parent=st.integers(1, 3),
    attack_fraction=st.sampled_from([0.0, 0.02, 0.1, 0.5, 1.0]),
    active_genome_cap=st.integers(1, 20),
)


class Log:
    """Shared, ordered log of label writes and submits."""

    def __init__(self, fail=lambda tx: False):
        self.ops: list[tuple[str, str]] = []
        self.fail = fail
        self.events = []
        self.backpressure = False

    def write(self, rec):  # GroundTruthWriter
        if self.fail(rec.event_id):
            raise OSError("label store unavailable")
        self.ops.append(("gt", rec.event_id))

    def submit(self, ev):  # ingest
        self.ops.append(("submit", ev.tx_id))
        self.events.append(ev)


def _h(salt: int, tx: str) -> int:
    return int(hashlib.sha1(f"{salt}:{tx}".encode()).hexdigest()[:8], 16)


def drive(cfg, seed, *, hours=4, step_s=120, legit=lambda i: 500, detect_salt=0, fail=lambda tx: False,
          on_tick=None):
    clock, log = ManualClock(T0), Log(fail)
    agent = AttackerAgent(cfg, clock, log, log, seed=seed, account_pool=POOL,
                          is_detected=lambda tx: _h(detect_salt, tx) % 3 == 0)
    agent.tick(T0)
    for i in range(int(hours * 3600 // step_s)):
        clock.advance(timedelta(seconds=step_s))
        agent.note_ingested(legit(i), clock.now())
        agent.tick(clock.now())
        if on_tick is not None:
            on_tick(agent)
    return agent, log


# ----------------------------------------------------------------- Property 61


# Feature: helix-fraud-immune-system, Property 61: Attacker seed determinism
@settings(max_examples=25)
@given(seed=st.integers(0, 2**32 - 1), cfg=configs, salt=st.integers(0, 1000))
def test_p61_attacker_seed_determinism(seed, cfg, salt):
    """**Validates: Requirements 20.13**"""
    a1, l1 = drive(cfg, seed, detect_salt=salt)
    a2, l2 = drive(cfg, seed, detect_salt=salt)
    assert a1.genomes() == a2.genomes()
    assert a1.campaigns() == a2.campaigns()
    assert l1.events == l2.events
    assert l1.ops == l2.ops


# ----------------------------------------------------------------- Property 62


# Feature: helix-fraud-immune-system, Property 62: Child genome validity
@settings(max_examples=200)
@given(parent=genomes(), seed=st.integers(0, 2**32 - 1))
def test_p62_child_differs_within_bounds_and_records_parent(parent, seed):
    """**Validates: Requirements 20.7**"""
    child = mutate(parent, np.random.default_rng(seed), "G999999")
    assert child.parent_id == parent.genome_id and child.ptype == parent.ptype
    assert child.generation == parent.generation + 1
    assert dict(child.params) != dict(parent.params)
    assert child.differs_from(parent)  # at least one Req 20.7 dimension
    spec = BOUNDS[child.ptype]
    assert set(child.params) == set(spec)
    assert all(spec[k].contains(v) for k, v in child.params.items())


# Feature: helix-fraud-immune-system, Property 62: Child genome validity
@settings(max_examples=25)
@given(seed=st.integers(0, 2**32 - 1), cfg=configs, salt=st.integers(0, 1000))
def test_p62_parent_selection_and_population_cap(seed, cfg, salt):
    """**Validates: Requirements 20.6, 20.15**"""
    cap = cfg.active_genome_cap
    initial = len(ALL_TYPES)

    def check(agent):
        active = agent.active_genomes()
        # Req 20.1 needs all 7 initial types, so the cap binds from the first evolution step on.
        evolved = any(g.evasion is not None for g in agent.genomes())
        assert len(active) <= (cap if evolved else max(cap, initial))
        scored = sorted((g for g in active if g.evasion is not None), key=lambda g: (-g.evasion, g.genome_id))
        assert agent.select_parents() == scored[: cfg.top_k]

    agent, _ = drive(cfg, seed, hours=6, detect_salt=salt, on_tick=check)
    by_id = {g.genome_id: g for g in agent.genomes()}
    for g in by_id.values():
        if g.parent_id is not None:
            assert g.parent_id in by_id and g.generation == by_id[g.parent_id].generation + 1
            assert agent.lineage(g.genome_id)[-2:] == (g.parent_id, g.genome_id)
    assert all(g.retired for g in by_id.values() if g not in agent.active_genomes())  # records kept


# ----------------------------------------------------------------- Property 63


# Feature: helix-fraud-immune-system, Property 63: Ground truth before injection
@settings(max_examples=30)
@given(seed=st.integers(0, 2**32 - 1), salt=st.integers(0, 1000), modulus=st.integers(1, 5))
def test_p63_ground_truth_written_before_submit(seed, salt, modulus):
    """**Validates: Requirements 20.10, 20.11**"""
    cfg = AttackerConfig(enabled=True, launch_interval_s=600, max_events=40, detection_window_s=600,
                         attack_fraction=1.0)
    fail = lambda tx: _h(salt, tx) % modulus == 0  # noqa: E731 - any pattern of write failures
    agent, log = drive(cfg, seed, hours=3, fail=fail)
    written: set[str] = set()
    for op, tx in log.ops:
        if op == "gt":
            written.add(tx)
        else:
            assert tx in written  # label strictly before submit
            assert not fail(tx)
    submitted = {e.tx_id for e in log.events}
    failed = {tx for c in agent.campaigns() for tx in c.withheld_ids}
    assert not (failed & submitted)
    assert all(fail(tx) for tx in failed)


# ----------------------------------------------------------------- Property 64


# Feature: helix-fraud-immune-system, Property 64: Attack rate cap
@settings(max_examples=30)
@given(
    seed=st.integers(0, 2**32 - 1),
    frac=st.sampled_from([0.0, 0.01, 0.02, 0.05, 0.3, 1.0]),
    legit=st.lists(st.integers(0, 400), min_size=1, max_size=40),
    interval=st.sampled_from([60.0, 300.0, 900.0]),
)
def test_p64_attack_rate_cap_per_stream_hour(seed, frac, legit, interval):
    """**Validates: Requirements 20.9**"""
    cfg = AttackerConfig(enabled=True, launch_interval_s=interval, max_events=100, attack_fraction=frac)
    legit_by_hour: dict[datetime, int] = defaultdict(int)
    inj_by_hour: dict[datetime, int] = defaultdict(int)
    seen = [0]
    clock_hour = [T0]

    def profile(i: int) -> int:
        n = legit[i % len(legit)]
        legit_by_hour[clock_hour[0]] += n
        return n

    def on_tick(agent):
        new = agent.injected_total - seen[0]
        inj_by_hour[clock_hour[0]] += new
        seen[0] = agent.injected_total

    step = 90
    clock, log = ManualClock(T0), Log()
    agent = AttackerAgent(cfg, clock, log, log, seed=seed, account_pool=POOL)
    for i in range(int(5 * 3600 // step)):
        clock.advance(timedelta(seconds=step))
        clock_hour[0] = clock.now().replace(minute=0, second=0, microsecond=0)
        agent.note_ingested(profile(i), clock.now())
        agent.tick(clock.now())
        on_tick(agent)
    for h, inj in inj_by_hour.items():
        assert inj <= frac * (legit_by_hour[h] + inj) + 1e-9


# ----------------------------------------------------------------- Property 65


# Feature: helix-fraud-immune-system, Property 65: No attack marker
@settings(max_examples=150)
@given(g=genomes(), seed=st.integers(0, 2**32 - 1), max_events=st.integers(1, 200))
def test_p65_campaign_events_ordered_and_schema_only(g, seed, max_events):
    """**Validates: Requirements 20.4, 20.8**"""
    ctx = CampaignContext(rng=np.random.default_rng(seed), start=T0, targets=TargetPool(POOL),
                          id_prefix=f"{seed}:C1", reporting_threshold=Decimal("10000"), max_events=max_events)
    evs = generate(g, ctx)
    assert 1 <= len(evs) <= max_events
    assert all(a.ts <= b.ts for a, b in zip(evs, evs[1:]))
    assert all(e.ts >= T0 for e in evs)
    assert len({e.tx_id for e in evs}) == len(evs)
    for e in evs:
        doc = json.loads(format(e))
        assert set(doc) <= SCHEMA_KEYS and set(REQUIRED_FIELDS) <= set(doc)
        text = json.dumps(doc).lower()
        for marker in ("campaign", "genome", "fraud", "attack", g.genome_id.lower()):
            assert marker not in text
        assert e.src != e.dst


# Feature: helix-fraud-immune-system, Property 65: No attack marker
@settings(max_examples=20)
@given(seed=st.integers(0, 2**32 - 1), cfg=configs)
def test_p65_injected_stream_non_decreasing(seed, cfg):
    """**Validates: Requirements 20.8**"""
    _, log = drive(cfg, seed, hours=4, legit=lambda i: 300 if i % 3 else 0)
    ts = [e.ts for e in log.events]
    assert all(a <= b for a, b in zip(ts, ts[1:]))
