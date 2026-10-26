"""Unit tests for the Immune_System (design §18, Requirement 21)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from helix.fusion.core import is_valid
from helix.immune import D, DIMS, INDEX, UNATTRIBUTED, ImmuneSystem, Reason, Status, feature_vector
from helix.immune.features import technique_tag
from helix.sim import ManualClock

from .helpers import T0, activate_one, fake_case, make_immune, seed_self, vec


# --- feature space ---------------------------------------------------------------------------


def _res(name: str, **features):
    return SimpleNamespace(name=name, ok=True, output=SimpleNamespace(features=features))


def test_feature_vector_normalises_and_marks_missing_as_nan():
    ev = SimpleNamespace(amount=1e9, ts=T0.replace(hour=6))
    f = feature_vector(ev, [_res("amount", z=5.0), _res("velocity", ratio=30.0), _res("counterparty", known=0.0)])
    assert f.shape == (D,)
    assert f[INDEX["amount_z"]] == pytest.approx(0.5)
    assert f[INDEX["log_amount"]] == pytest.approx(1.0)
    assert f[INDEX["hour_sin"]] == pytest.approx(1.0)  # 06:00 -> sin = 1
    assert f[INDEX["velocity_ratio"]] == 1.0  # clipped
    assert f[INDEX["counterparty_unknown"]] == 1.0
    assert np.isnan(f[INDEX["geo_speed_ratio"]])  # no Geo result: missing
    assert all(f[INDEX[d]] == 0.0 for d in DIMS if d.startswith("motif_"))


def test_failed_sensor_leaves_its_dimension_missing():
    ev = SimpleNamespace(amount=100.0, ts=T0)
    bad = SimpleNamespace(name="amount", ok=False, output=None)
    assert np.isnan(feature_vector(ev, [bad])[INDEX["amount_z"]])


def test_technique_tags():
    assert technique_tag("FAN_OUT") == "HX-T0002"
    assert technique_tag("ROUND_TRIP") == "HX-T0001"
    assert technique_tag("hx-t0042") == "HX-T0042"
    assert technique_tag("nope") is None


# --- clonal selection ---------------------------------------------------------------------------


def test_miss_creates_configured_number_of_held_clones_covering_the_signature():
    imm = make_immune(candidates_per_trigger=7, self_set_min=1_000)
    sig = vec(log_amount=0.9, hour_sin=0.98, hour_cos=0.2)
    imm.observe("x", sig)
    out = imm.on_miss("x")
    assert len(out) == 7
    assert all(a.status is Status.HELD for a in out)
    r = imm.cfg.mutation_rate
    m = ~np.isnan(sig)
    for a in out:
        assert a.matches_vector(sig)  # every clone still contains its origin event
        assert np.all(np.abs(a.center[m] - sig[m]) <= r * np.abs(sig[m]) + 1e-12)  # centre jitter <= ±r
        assert a.origin == "miss:x" and a.tags == (UNATTRIBUTED,)
    assert imm.triggers[-1].candidates == tuple(a.ab_id for a in out)


def test_miss_without_cached_features_records_no_signature():
    imm = make_immune()
    assert imm.on_miss("unknown") == []
    assert imm.triggers[-1].note == "no_signature"


def test_tagger_supplies_genome_technique_tags():
    imm = make_immune(self_set_min=1_000)
    imm.tagger = lambda tx: ["MULE_CHAIN"]
    imm.observe("x", vec(log_amount=0.9))
    assert {a.tags for a in imm.on_miss("x")} == {("HX-T0005",)}


def test_clones_are_deterministic_for_a_seed():
    def centres(seed: int):
        imm = make_immune(seed=seed, self_set_min=1_000)
        imm.observe("x", vec(log_amount=0.6, hour_sin=0.3))
        return [a.center.tolist() for a in imm.on_miss("x")]

    assert centres(3) == centres(3)
    assert centres(3) != centres(4)


# --- negative selection ---------------------------------------------------------------------------


def test_candidates_held_until_self_set_reaches_minimum_then_selected():
    imm = make_immune(self_set_min=10)
    seed_self(imm, 4)
    held = activate_one(imm, "atk", 0.9)
    assert all(a.status is Status.HELD for a in held)
    seed_self(imm, 10, prefix="t")  # reaches the minimum; observe() ticks
    assert all(a.status is Status.ACTIVE for a in held)
    assert all(a.self_match_rate == 0.0 for a in held)


def test_candidate_matching_self_is_discarded_with_rate():
    imm = make_immune(self_set_min=5)
    seed_self(imm, 10, value=0.9)  # self looks exactly like the attack
    out = activate_one(imm, "atk", 0.9)
    assert all(a.status is Status.DISCARDED for a in out)
    d = imm.discards[-1]
    assert d.reason is Reason.SELF_MATCH and d.self_match_rate == pytest.approx(1.0)


def test_held_candidate_expires():
    clock = ManualClock(T0)
    imm = make_immune(clock=clock, self_set_min=1_000, hold_expiry_s=60)
    out = activate_one(imm, "atk", 0.9)
    clock.advance(timedelta(seconds=61))
    imm.tick()
    assert all(a.status is Status.DISCARDED and a.ended[1] is Reason.EXPIRED for a in out)
    assert {d.ab_id for d in imm.discards if d.reason is Reason.EXPIRED} == {a.ab_id for a in out}


def test_canary_events_and_confirmed_fraud_cases_are_excluded_from_self():
    imm = make_immune()
    imm.is_canary = lambda acct: acct == "CANARY"
    imm.observe("c1", vec(log_amount=0.2), SimpleNamespace(src="A", dst="CANARY"))
    imm.observe("n1", vec(log_amount=0.2), SimpleNamespace(src="A", dst="B"))
    imm.observe("n2", vec(log_amount=0.2), SimpleNamespace(src="A", dst="B"))
    assert set(imm.self_ids()) == {"n1", "n2"}
    imm.on_label(fake_case([], tx_ids=["n2"]), "CONFIRMED_FRAUD")
    assert imm.self_ids() == ["n1"]
    imm.observe("n2", vec(log_amount=0.2))  # never re-admitted
    assert imm.self_ids() == ["n1"]


# --- matching, pool, labels ------------------------------------------------------------------------


def test_active_antibody_emits_fraud_mass_and_nan_never_matches():
    imm = make_immune(candidates_per_trigger=1)
    seed_self(imm, 5)
    (ab,) = activate_one(imm, "atk", 0.9)
    hit = vec(log_amount=0.9, hour_sin=0.5, hour_cos=0.5)
    (m,) = imm.evaluate(hit)
    assert m.contributor_id == f"antibody:{ab.ab_id}"
    assert (m.mass.f, m.mass.l) == (imm.cfg.antibody_mass, 0.0) and is_valid(m.mass)
    assert imm.evaluate(vec(log_amount=np.nan, hour_sin=0.5, hour_cos=0.5)) == []
    assert ab.matches == 1


def test_false_positive_limit_retires_and_lineage_is_kept():
    imm = make_immune(candidates_per_trigger=1, fp_limit=1)
    seed_self(imm, 5)
    (ab,) = activate_one(imm, "atk", 0.9)
    imm.on_label(fake_case([ab.ab_id]), "FALSE_POSITIVE")
    assert ab.active
    imm.on_label(fake_case([ab.ab_id], case_id="C-2"), "FALSE_POSITIVE")
    assert ab.status is Status.RETIRED and ab.ended[1] is Reason.FP_LIMIT
    rec = {r["ab_id"]: r for r in imm.lineage()}[ab.ab_id]
    assert rec["retired_reason"] == "false_positive_limit" and rec["origin"] == "miss:atk"
    assert rec["false_positive_matches"] == 2
    assert imm.evaluate(vec(log_amount=0.9, hour_sin=0.5, hour_cos=0.5)) == []


def test_pool_full_retires_lowest_affinity():
    clock = ManualClock(T0)
    imm = make_immune(clock=clock, candidates_per_trigger=1, pool_max=2)
    seed_self(imm, 5)
    (a,) = activate_one(imm, "x1", 0.5)
    clock.advance(timedelta(seconds=1))
    (b,) = activate_one(imm, "x2", 0.7)
    imm.on_label(fake_case([a.ab_id]), "CONFIRMED_FRAUD")  # a: affinity 1 > b: 0
    clock.advance(timedelta(seconds=1))
    (c,) = activate_one(imm, "x3", 0.9)
    assert [x.ab_id for x in imm.pool()] == [a.ab_id, c.ab_id]
    assert b.status is Status.RETIRED and b.ended[1] is Reason.CAPACITY


def test_low_risk_confirmed_fraud_case_triggers_clonal_selection():
    imm = make_immune(candidates_per_trigger=2)
    seed_self(imm, 5)
    imm.observe("k1", vec(log_amount=0.8, hour_sin=0.5, hour_cos=0.5))
    imm.observe("k2", vec(log_amount=0.85, hour_sin=0.5, hour_cos=0.5))
    out = imm.on_label(fake_case([], tx_ids=["k1", "k2"], risk=0.1, case_id="C-9"), "CONFIRMED_FRAUD",
                       alert_threshold=0.5)
    assert len(out) == 2 and all(a.origin == "case:C-9" and a.active for a in out)
    assert all(a.matches_vector(imm.features_of("k1")) and a.matches_vector(imm.features_of("k2")) for a in out)
    assert imm.on_label(fake_case([], tx_ids=["k1"], risk=0.9), "CONFIRMED_FRAUD", alert_threshold=0.5) == []


def test_snapshot_round_trip_preserves_behaviour():
    imm = make_immune(candidates_per_trigger=2)
    seed_self(imm, 6)
    activate_one(imm, "atk", 0.9)
    copy = ImmuneSystem.from_dict(imm.to_dict(), imm.cfg, ManualClock(T0))
    probe = vec(log_amount=0.9, hour_sin=0.5, hour_cos=0.5)
    assert [m.ab_id for m in copy.evaluate(probe, record=False)] == [m.ab_id for m in imm.evaluate(probe, record=False)]
    imm.observe("y", vec(log_amount=0.3))
    copy.observe("y", vec(log_amount=0.3))
    assert [a.center.tolist() for a in copy.on_miss("y")] == [a.center.tolist() for a in imm.on_miss("y")]
