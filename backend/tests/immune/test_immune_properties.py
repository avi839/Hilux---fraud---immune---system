"""Hypothesis property tests for the Immune_System (design Properties 66-69).

All tests are deterministic: Hypothesis runs with ``derandomize=True`` and no example
database, and the Immune_System, generator and campaign are seeded.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from helix.attacker import PatternType, default_genome
from helix.immune import Reason, Status
from helix.sim import ManualClock

from .harness import make_stream, run_loop
from .helpers import T0, fake_case, make_immune, vec

pytestmark = pytest.mark.property

FAST = settings(max_examples=60, deadline=None, derandomize=True, database=None)

unit = st.floats(0.0, 1.0, allow_nan=False)


def oracle_match(ab, fv: np.ndarray) -> bool:
    """Independent box check: every masked dimension present and within the half-width."""
    for c, h, m, f in zip(ab.center, ab.half_width, ab.mask, fv):
        if m and (np.isnan(f) or abs(f - c) > h):
            return False
    return True


# --- Property 66 ------------------------------------------------------------------------------

self_event = st.tuples(unit, st.sampled_from(["plain", "canary", "confirmed"]))


@FAST
@given(
    attack=unit,
    near=st.lists(st.floats(-0.05, 0.05, allow_nan=False), max_size=8),
    others=st.lists(self_event, max_size=25),
    self_min=st.integers(1, 15),
    limit=st.sampled_from([0.0, 0.05, 0.1, 0.25, 0.5, 1.0]),
    n_clones=st.integers(1, 4),
    seed=st.integers(0, 3),
)
def test_p66_negative_selection(attack, near, others, self_min, limit, n_clones, seed):
    """Property 66: a candidate is activated only when |Self_Set| >= min and its self-match
    rate is strictly below the limit, and discarded with its rate recorded when the rate is
    at or above the limit; Canary and CONFIRMED_FRAUD events never count as self.

    **Validates: Requirements 21.3, 21.4, 21.7**
    """
    imm = make_immune(seed=seed, self_set_min=self_min, self_match_limit=limit, candidates_per_trigger=n_clones)
    imm.is_canary = lambda acct: acct == "CANARY"
    events = [(min(1.0, max(0.0, attack + d)), "plain") for d in near] + list(others)
    confirmed = []
    for i, (x, kind) in enumerate(events):
        tx = f"s{i}"
        ev = SimpleNamespace(src="A", dst="CANARY" if kind == "canary" else "B")
        imm.observe(tx, vec(log_amount=x, hour_sin=0.5, hour_cos=0.5), ev)
        if kind == "confirmed":
            confirmed.append(tx)
    imm.on_label(fake_case([], tx_ids=confirmed), "CONFIRMED_FRAUD")

    self_vecs = [vec(log_amount=x, hour_sin=0.5, hour_cos=0.5) for x, k in events if k == "plain"]
    assert imm.self_size == len(self_vecs)

    imm.observe("atk", vec(log_amount=attack, hour_sin=0.5, hour_cos=0.5))
    clones = imm.on_miss("atk")
    imm.tick()
    assert len(clones) == n_clones
    discards = {d.ab_id: d for d in imm.discards}
    for ab in clones:
        if len(self_vecs) < self_min:
            assert ab.status is Status.HELD
            continue
        rate = sum(oracle_match(ab, f) for f in self_vecs) / len(self_vecs)
        assert ab.self_match_rate == pytest.approx(rate)
        if rate < limit:
            assert ab.status is Status.ACTIVE
        else:
            assert ab.status is Status.DISCARDED
            d = discards[ab.ab_id]
            assert d.reason is Reason.SELF_MATCH and d.self_match_rate == pytest.approx(rate)


# --- Property 67 ------------------------------------------------------------------------------

op = st.one_of(
    st.tuples(st.just("activate"), st.floats(0.3, 1.0, allow_nan=False)),
    st.tuples(st.just("label"), st.integers(0, 10), st.sampled_from(["CONFIRMED_FRAUD", "FALSE_POSITIVE"])),
    st.tuples(st.just("advance"), st.integers(0, 400)),
)


@FAST
@given(ops=st.lists(op, min_size=1, max_size=40), pool_max=st.integers(1, 4), fp_limit=st.integers(0, 3))
def test_p67_pool_cap_and_capacity_retirement(ops, pool_max, fp_limit):
    """Property 67: the active pool never exceeds the maximum, and every capacity retirement
    removes an antibody with the lowest affinity (CONFIRMED_FRAUD minus FALSE_POSITIVE label
    matches within the window), the earliest created among ties.

    **Validates: Requirements 21.9, 21.10**
    """
    window = 600
    clock = ManualClock(T0)
    imm = make_immune(clock=clock, self_set_min=1, candidates_per_trigger=1, pool_max=pool_max,
                      fp_limit=fp_limit, affinity_window_s=window)
    imm.observe("self", vec(log_amount=0.0, hour_sin=0.5, hour_cos=0.5))  # never near an attack value
    labels: dict[str, list[tuple[object, int]]] = {}  # ab_id -> [(time, +1 | -1)]

    def affinity(ab_id: str) -> int:
        now = clock.now()
        lo = now - timedelta(seconds=window)
        return sum(s for t, s in labels.get(ab_id, ()) if lo <= t <= now)

    for k, o in enumerate(ops):
        if o[0] == "activate":
            before = list(imm.pool())
            n_cap = sum(1 for _, _, r in imm.retirements if r is Reason.CAPACITY)
            imm.observe(f"x{k}", vec(log_amount=o[1], hour_sin=0.5, hour_cos=0.5))
            (ab,) = imm.on_miss(f"x{k}")
            imm.tick()
            assert ab.status is Status.ACTIVE
            new_cap = [i for i, _, r in imm.retirements if r is Reason.CAPACITY][n_cap:]
            if len(before) >= pool_max:
                (victim,) = new_cap
                best = min(before, key=lambda a: (affinity(a.ab_id), a.created_at, a.seq))
                assert (affinity(victim), imm.antibodies[victim].created_at) == (
                    affinity(best.ab_id), best.created_at)
                assert victim == best.ab_id
            else:
                assert new_cap == []
        elif o[0] == "label":
            pool = imm.pool()
            if not pool:
                continue
            ab = pool[o[1] % len(pool)]
            labels.setdefault(ab.ab_id, []).append((clock.now(), 1 if o[2] == "CONFIRMED_FRAUD" else -1))
            imm.on_label(fake_case([ab.ab_id], case_id=f"C-{k}"), o[2])
            if o[2] == "FALSE_POSITIVE" and ab.fp_count > fp_limit:
                assert ab.status is Status.RETIRED and ab.ended[1] is Reason.FP_LIMIT
        else:
            clock.advance(timedelta(seconds=o[1]))
        assert len(imm.pool()) <= pool_max
        assert all(a.status is Status.ACTIVE for a in imm.pool())


# --- Property 68 ------------------------------------------------------------------------------

probe = st.lists(st.one_of(st.none(), unit), min_size=3, max_size=3)


@FAST
@given(how=st.sampled_from(["capacity", "false_positive"]), value=st.floats(0.3, 1.0, allow_nan=False),
       probes=st.lists(probe, max_size=10), seed=st.integers(0, 3))
def test_p68_retired_antibodies_are_silent(how, value, probes, seed):
    """Property 68: an antibody retired by capacity or the false-positive limit emits no
    contribution for any later feature vector.

    **Validates: Requirements 21.11**
    """
    imm = make_immune(seed=seed, self_set_min=1, candidates_per_trigger=1, pool_max=1, fp_limit=0)
    imm.observe("self", vec(log_amount=0.0, hour_sin=0.5, hour_cos=0.5))
    imm.observe("x", vec(log_amount=value, hour_sin=0.5, hour_cos=0.5))
    (ab,) = imm.on_miss("x")
    imm.tick()
    centre = np.where(ab.mask, ab.center, np.nan)
    assert ab.contributor_id in {m.contributor_id for m in imm.evaluate(centre, record=False)}
    if how == "capacity":
        imm.observe("y", vec(log_amount=min(1.0, value + 0.01), hour_sin=0.5, hour_cos=0.5))
        imm.on_miss("y")
        imm.tick()
        assert ab.ended[1] is Reason.CAPACITY
    else:
        imm.on_label(fake_case([ab.ab_id]), "FALSE_POSITIVE")
        assert ab.ended[1] is Reason.FP_LIMIT
    assert ab.status is Status.RETIRED
    vectors = [centre, ab.center] + [vec(log_amount=p[0] if p[0] is not None else np.nan,
                                         hour_sin=p[1] if p[1] is not None else np.nan,
                                         hour_cos=p[2] if p[2] is not None else np.nan) for p in probes]
    for fv in vectors:
        assert ab.contributor_id not in {m.contributor_id for m in imm.evaluate(fv)}
        assert ab.contributor_id not in {c.contributor_id for c in imm.contributions(fv)}
    assert ab.ab_id in {r["ab_id"] for r in imm.lineage()}  # the record survives retirement


# --- Property 69 ------------------------------------------------------------------------------

TYPES = [t for t in PatternType if t.name != "ROUND_TRIP"]  # ROUND_TRIP is an alias


@settings(max_examples=6, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(ptype=st.sampled_from(TYPES), seed=st.integers(0, 3))
def test_p69_immune_learning_never_loses_detections(ptype, seed):
    """Property 69: replaying the same campaign (same genome and seed) after activating
    antibodies from its misses places at least as many events in Cases within the detection
    deadline as before.

    **Validates: Requirements 21.14**
    """
    stream = make_stream(default_genome(ptype, f"G-{ptype.value}"), seed)
    before = run_loop(stream, learn_from_misses=False)
    trained = run_loop(stream)
    after = run_loop(stream, trained.immune, learn_from_misses=False)
    assert len(after.detected) >= len(before.detected)
