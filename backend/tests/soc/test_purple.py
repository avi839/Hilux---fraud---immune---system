"""Purple_Team_Exercise, Exercise_Scorecard and isolation (helix.soc.purple, Req 37.1-37.4)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from functools import lru_cache
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.api import AppState
from helix.attacker.genome import PatternType, default_genome
from helix.sim.clock import ManualClock
from helix.sim.generator import SyntheticGenerator
from helix.sim.ground_truth import GroundTruthRecord, GTSource, InMemoryGroundTruth
from helix.soc import attach_rules, soc_config
from helix.soc.purple import (
    METRIC_KEYS,
    ExerciseSpecError,
    PurpleTeam,
    ScorecardStore,
    compare,
    isolate,
    isolate_component,
    live_fingerprint,
    parse_spec,
)
from helix.soc.resilience import FaultySensor, ResilienceMode
from helix.storage import Storage

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
RULE = """\
id: R-BIG
title: Large payment
techniques: [HX-T0008]
detection:
  big:
    tx.amount|gte: 5000
  condition: big
mass:
  fraud: 0.4
"""
FAST_TYPES = ["FAN_OUT", "FAN_IN", "STRUCTURING", "MULE_CHAIN", "CARD_TESTING", "ROUND_TRIP_CYCLE",
              "ACCOUNT_TAKEOVER"]


def const_clock() -> float:
    return 0.0


class ToyIndicators:
    """Stand-in Indicator_Store implementing the ``copy()`` protocol."""

    def __init__(self, items=None) -> None:
        self.items = dict(items or {})

    def copy(self) -> ToyIndicators:
        return ToyIndicators(self.items)


def build_live(events: int = 120, seed: int = 1):
    cfg = soc_config()
    cfg = cfg.model_copy(update={"generator": cfg.generator.model_copy(update={"accounts": 200})})
    gt = InMemoryGroundTruth()
    state = AppState.build(cfg, ManualClock(T0), immune=True, sensor_clock=const_clock, gt_writer=gt)
    helix = state.helix
    eng = attach_rules(helix)
    eng.submit(RULE)
    eng.activate("R-BIG")
    gen = SyntheticGenerator(cfg.generator, ManualClock(T0), gt, seed=seed)
    for _ in range(events):
        helix.process(next(gen))
    gt.write(GroundTruthRecord(event_id="X-1", label="FRAUD", source=GTSource.ATTACKER, written_at=T0))
    # one labelled Case so learner weights and the antibody feedback path are non-trivial
    cid = sorted(helix.cases.cases)[0]
    helix.open_case(cid, "ana")
    helix.label(cid, "ana", "FALSE_POSITIVE")
    return state, gt


@lru_cache(maxsize=1)
def shared_live():
    return build_live()


def test_isolated_copy_starts_equal_and_is_independent():
    state, _ = build_live(60)
    live = state.helix
    mode = ResilienceMode()
    mode.install_sensors(live)  # wrappers are removed in the copy
    ind = ToyIndicators({"ip:1.2.3.4": "c2"})
    touched = []
    iso = isolate(live, indicators=ind, installers=[lambda cp, i: touched.append((cp, i))])
    cp = iso.helix
    assert touched == [(cp, iso.indicators)] and cp.indicators is iso.indicators
    assert iso.indicators is not ind and iso.indicators.items == ind.items
    assert cp.snapshot_state(live.seq_no) == live.snapshot_state()  # DNA and CPD state
    assert cp.weights == live.weights and cp.weights is not live.weights
    def js(d):  # feature vectors hold NaN for missing dimensions (NaN != NaN)
        return json.dumps(d, sort_keys=True, default=str)

    assert js(cp.immune.to_dict()) == js(live.immune.to_dict()) and cp.immune is not live.immune
    assert [r.rule_id for r in cp.rules.active_rules()] == ["R-BIG"] and cp.rules is not live.rules
    assert [e.tx_id for e in cp.graph.edges()] == [e.tx_id for e in live.graph.edges()]
    assert cp.cases.cases == {} and cp.storage is None and cp.clock is not live.clock
    assert not any(isinstance(s, FaultySensor) for s in cp.stage1.sensors)
    assert all(a is not b for a, b in zip(cp.stage1.sensors, live.stage1.sensors))
    auth = next(s for s in cp.stage1.sensors if s.name == "auth")
    assert auth.history is cp.auth and cp.auth is not live.auth
    assert cp.profiles is not live.profiles and cp.cpd is not live.cpd


def test_isolate_component_protocols():
    class Snap:
        def __init__(self, v):
            self.v = v

        def snapshot(self):
            return {"v": list(self.v)}

        @classmethod
        def from_snapshot(cls, d):
            return cls(d["v"])

    s = Snap([1, 2])
    c = isolate_component(s)
    assert c.v == [1, 2] and c.v is not s.v
    d = {"a": [1]}
    assert isolate_component(d) == d and isolate_component(d)["a"] is not d["a"]  # dict.copy is shallow: deepcopy
    assert isolate_component(None) is None

    class Same:
        def copy(self):
            return self

    with pytest.raises(ValueError):
        isolate_component(Same())


def test_exercise_scorecard_metrics_and_live_state_untouched():
    state, gt = build_live()
    live = state.helix
    before, gt_before = live_fingerprint(live), copy.deepcopy(gt.records)
    purple = PurpleTeam(live)
    spec = parse_spec({"name": "baseline", "campaigns": [{"ptype": "FAN_OUT", "seed": 1},
                                                          {"ptype": "STRUCTURING", "seed": 2, "count": 2}],
                       "background_events": 20})
    sc = purple.run(spec)
    assert live_fingerprint(live) == before and gt.records == gt_before
    m = sc.metrics
    assert set(m) == set(METRIC_KEYS)
    assert m["campaigns"] == 3 and m["background_events"] == 20
    assert m["events"] == sum(c["events"] for c in sc.campaigns) > 0
    assert 0 <= m["detected_events"] <= m["events"] and m["detection_rate"] == m["detected_events"] / m["events"]
    assert m["alert_volume"] >= m["false_positives"] >= 0
    assert m["detected_campaigns"] == sum(c["detected"] for c in sc.campaigns)
    lats = sorted(c["detection_latency_s"] for c in sc.campaigns if c["detected"])
    if lats:
        assert m["median_detection_latency_s"] is not None and lats[0] <= m["median_detection_latency_s"] <= lats[-1]
    fan = sc.techniques["HX-T0002"]  # Fan-out dispersal
    assert fan["campaigns"] == 1 and fan["covered"] and "motif:FAN_OUT" in fan["contributors"]
    assert "rule:R-BIG" in sc.techniques["HX-T0008"]["contributors"]  # the copied active rule is deployed
    untouched = [t for t, r in sc.techniques.items() if r["campaigns"] == 0]
    assert untouched and all(sc.techniques[t]["detection_rate"] is None for t in untouched)
    assert purple.store.list()[0].scorecard_id == sc.scorecard_id == "SC-1"
    # deterministic: the same exercise on the same state scores the same
    sc2 = purple.run(spec)
    assert sc2.scorecard_id == "SC-2" and sc2.metrics == sc.metrics
    assert [c["detected_events"] for c in sc2.campaigns] == [c["detected_events"] for c in sc.campaigns]


def test_compare_reports_metric_and_technique_differences():
    state, _ = shared_live()
    purple = PurpleTeam(state.helix)
    a = purple.run(parse_spec({"campaigns": [{"ptype": "FAN_OUT"}]}))
    b = purple.run(parse_spec({"campaigns": [{"ptype": "FAN_OUT"}, {"ptype": "CARD_TESTING", "count": 2}]}))
    d = purple.compare(a.scorecard_id, b.scorecard_id)
    assert d["a"] == a.scorecard_id and d["b"] == b.scorecard_id
    assert set(d["metrics"]) == set(METRIC_KEYS)
    assert d["metrics"]["campaigns"] == {"a": 1, "b": 3, "delta": 2}
    assert d["metrics"]["events"]["delta"] == b.metrics["events"] - a.metrics["events"]
    assert set(d["techniques"]) == set(a.techniques) == set(b.techniques)
    card = d["techniques"]["HX-T0007"]  # Card testing
    assert card["campaigns"] == {"a": 0, "b": 2, "delta": 2}
    assert card["detection_rate"]["a"] is None and card["detection_rate"]["delta"] is None
    assert card["covered"]["changed"] is False and card["contributors"]["delta"] == 0
    with pytest.raises(KeyError):
        purple.compare(a.scorecard_id, "SC-999")


def test_scorecards_persist_in_duckdb(tmp_path):
    state, _ = shared_live()
    s = Storage(tmp_path / "helix.duckdb").open()
    try:
        store = ScorecardStore(s)
        sc = PurpleTeam(state.helix, store=store).run(parse_spec({"campaigns": [{"ptype": "MULE_CHAIN"}]}))
        again = ScorecardStore(s)
        got = again.get(sc.scorecard_id)
        assert got.to_dict() == sc.to_dict() and again.next_seq() == 2 and again.storage_errors == 0
        with pytest.raises(ValueError):
            again.save(got)
    finally:
        s.close()


def test_spec_validation_names_every_problem():
    with pytest.raises(ExerciseSpecError) as e:
        parse_spec({"campaigns": [{"ptype": "NOPE"}, {"ptype": "FAN_OUT", "params": {"n_counterparties": 999,
                                                                                    "colour": 1}},
                                  {"ptype": "MULE_CHAIN", "count": 0}],
                    "background_events": -1, "seed": "x", "gap_s": -5})
    fields = {f for f, _ in e.value.problems}
    assert fields == {"campaigns[0].ptype", "campaigns[1].params.n_counterparties", "campaigns[1].params.colour",
                      "campaigns[2].count", "background_events", "seed", "gap_s"}
    with pytest.raises(ExerciseSpecError, match="non-empty"):
        parse_spec({"campaigns": []})
    with pytest.raises(ExerciseSpecError, match="at most 50"):
        parse_spec({"campaigns": [{"ptype": "FAN_OUT", "count": 30}, {"ptype": "FAN_IN", "count": 30}]})
    g = default_genome(PatternType.FAN_IN, "G7")
    att = SimpleNamespace(genome=lambda gid: {"G7": g}[gid])
    spec = parse_spec({"campaigns": [{"genome_id": "G7", "seed": 3}]}, att)
    assert spec.campaigns[0].ptype is PatternType.FAN_IN and spec.campaigns[0].genome_id == "G7"
    with pytest.raises(ExerciseSpecError, match="unknown attacker genome"):
        parse_spec({"campaigns": [{"genome_id": "G8"}]}, att)


# --- property: isolation --------------------------------------------------------------------------------

campaign = st.fixed_dictionaries({"ptype": st.sampled_from(FAST_TYPES), "seed": st.integers(0, 2**16)},
                                 optional={"count": st.integers(1, 2)})


@settings(max_examples=12, deadline=None, derandomize=True, database=None)
@given(campaigns=st.lists(campaign, min_size=1, max_size=3), background=st.integers(0, 15),
       seed=st.integers(0, 1000), gap=st.sampled_from([0, 60, 600]))
def test_exercises_leave_live_state_unchanged(campaigns, background, seed, gap):
    """For all Purple_Team_Exercises, the live Cases, Sensor_Weights, Antibody_Pool,
    Behavioural_DNA (and CPD, graph, rules), the Indicator_Store and the Ground_Truth_Store are
    equal before and after the exercise.

    **Validates: Requirements 37.1, 37.2**
    """
    state, gt = shared_live()
    live = state.helix
    ind = ToyIndicators({"ip:10.0.0.1": "tor"})

    def poke(cp, indicators):  # the copy may be mutated freely
        indicators.items["touched"] = True
        cp.weights["amount"] = 0.0

    purple = PurpleTeam(live, indicators=ind, installers=[poke])
    before, gt_before, ind_before = live_fingerprint(live), list(gt.records), dict(ind.items)
    sc = purple.run(parse_spec({"campaigns": campaigns, "background_events": background, "seed": seed,
                                "gap_s": gap}))
    assert sc.metrics["campaigns"] == sum(c.get("count", 1) for c in campaigns)
    assert live_fingerprint(live) == before
    assert gt.records == gt_before
    assert ind.items == ind_before
