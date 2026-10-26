"""End-to-end immune loop: an attack evades HELIX, the miss is detected, antibodies are
created and activated, and replaying the same attack now places the missed event in a Case
with an Antibody contribution in its stage-1 assessment (Req 21.1, 21.2, 21.3, 21.8, 21.14)."""

from __future__ import annotations

from helix.attacker import PatternType, default_genome
from helix.fusion import ContributorType
from helix.immune import Status

from .harness import make_stream, run_loop


def test_missed_attack_is_caught_on_replay_after_antibody_activation():
    stream = make_stream(default_genome(PatternType.MULE_CHAIN, "G-MULE"), seed=0)
    attack = {e.tx_id for e in stream.attack}

    baseline = run_loop(stream, learn_from_misses=False)  # no learning: what HELIX alone catches
    trained = run_loop(stream)  # same stream, misses feed the Immune_System
    missed = trained.missed
    assert missed, "the campaign should evade at least once in this scenario"
    assert set(missed) <= attack
    assert len(missed) == len(set(missed))  # each miss recorded once (Req 21.1)
    assert set(missed).isdisjoint(trained.detected)

    imm = trained.immune
    origins = {f"miss:{tx}" for tx in missed}
    created = [a for a in imm.antibodies.values() if a.origin in origins]
    assert len(created) == imm.cfg.candidates_per_trigger * len(missed)  # clonal selection (Req 21.2)
    active = [a for a in created if a.status is Status.ACTIVE]
    assert active, "negative selection should keep at least one clone"
    assert all(a.self_match_rate is not None and a.self_match_rate < imm.cfg.self_match_limit for a in active)
    assert set(missed).isdisjoint(imm.self_ids())  # a recorded miss is not "self"

    after = run_loop(stream, imm, learn_from_misses=False)  # replay with the trained pool
    assert len(after.detected) > len(baseline.detected)
    caught = [tx for tx in missed if tx in after.detected]
    assert caught, "a previously missed event is now placed in a Case within the deadline"
    for tx in caught:
        case = after.helix.cases.cases[after.helix.cases.case_of(tx)]
        kinds = {c.kind for c in case.assessments[tx].contributions}
        assert ContributorType.ANTIBODY in kinds  # the antibody joined stage-1 fusion (Req 21.8)
