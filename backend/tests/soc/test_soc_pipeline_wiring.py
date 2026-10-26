"""SOC components wired into the detection loop behind ``soc.enabled`` (task 18.2; Req 18.9,
30.5, 32-35): Canary / Indicator mass in stage 1, Canary Cases and the Prioritiser exception,
Canary exclusion from DNA / Self_Set / the generator, attacker Canary population and technique
tags, the Playbook_Runner on Case updates and the Audit_Ledger hooks. With soc.enabled false
nothing changes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.api import AppState
from helix.attacker import attach_attacker
from helix.cases import Label, Placement, Prioritiser
from helix.cases.prioritiser import CaseKey
from helix.codec import TransactionEvent
from helix.config import HelixConfig
from helix.core import Helix
from helix.fusion import Mass
from helix.motifs import MotifEngine
from helix.sensors import SensorOutput
from helix.sim.clock import ManualClock
from helix.sim.generator import SyntheticGenerator
from helix.sim.ground_truth import InMemoryGroundTruth
from helix.soc import soc_config
from helix.soc.canaries import CANARY_CONTRIBUTOR_ID, CanaryKind, CanaryRegistry
from helix.soc.intel import TLP, Indicator, IndicatorStore, IndicatorType
from helix.soc.ledger import AuditLedger
from helix.soc.playbooks import PlaybookRunner

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


class Hot:
    """Test sensor: every event is strongly suspicious."""

    name = "fixed"

    def evaluate(self, ev, dna_before, ctx):
        return SensorOutput(Mass(0.9, 0.0, 0.1), {})


def tx(tx_id: str, src: str = "ACC000001", dst: str = "ACC000002", amount: str = "25.00",
       ts: datetime = T0, device_id: str | None = None) -> TransactionEvent:
    return TransactionEvent(tx_id=tx_id, ts=ts, src=src, dst=dst, amount=Decimal(amount), currency="EUR",
                            device_id=device_id)


def soc_state(**kw) -> AppState:
    kw.setdefault("learner", False)
    return AppState.build(soc_config(), ManualClock(T0), sensor_clock=lambda: 0.0, **kw)


def categories(ledger) -> list[str]:
    return [e.category for e in ledger.entries()]


# --- wiring -------------------------------------------------------------------------------


def test_enable_soc_wires_every_component_into_the_pipeline():
    state = soc_state(immune=True)
    h = state.helix
    assert h.canaries is state.canaries and h.playbooks is state.playbook_runner
    assert h.audit is state.ledger and h.ledger is state.ledger and h.indicators is state.indicators
    assert h.rules.audit is state.ledger
    some = state.canaries.ids(CanaryKind.ACCOUNT)[0]
    assert state.immune.is_canary(some) and not state.immune.is_canary("ACC000001")
    state.enable_soc()  # idempotent: no second feedback hook
    hooks = [x for x in h.feedback_hooks if getattr(x, "audit_ledger", None) is state.ledger]
    assert len(hooks) == 1


def test_canary_touch_fuses_mass_forces_case_and_skips_dna_and_self_set():
    state = soc_state(immune=True)
    h = state.helix
    payee = state.canaries.ids(CanaryKind.PAYEE)[0]
    h.process(tx("T0", ts=T0 - timedelta(minutes=5)))  # a normal event for the source account
    out = h.process(tx("T1", dst=payee))
    ids = [c.contributor_id for c in out.stage1.contributions]
    assert CANARY_CONTRIBUTOR_ID in ids and out.stage1.mass.f >= 0.95
    assert out.case_id is not None and out.case_id in h.cases.canary_cases
    assert h.cases.cases[out.case_id].placement is Placement.ACTIVE
    assert not out.applied  # Req 33.6: not applied to Behavioural_DNA
    assert "T1" not in state.immune._self_slot
    assert [x.event_id for x in state.canaries.hits()] == ["T1"]
    chain = h.chain(out.case_id)
    assert any(e.contributor_id == CANARY_CONTRIBUTOR_ID and str(e.ctype) == "CANARY" for e in chain)


def test_canary_device_reference_also_counts():
    state = soc_state()
    dev = state.canaries.ids(CanaryKind.DEVICE)[0]
    out = state.helix.process(tx("T1", device_id=dev))
    assert out.case_id in state.helix.cases.canary_cases and not out.applied


def test_indicator_contribution_uses_extend_stage1():
    state = soc_state()
    state.indicators.add(Indicator("MULE-9", IndicatorType.ACCOUNT, 100, "test", T0 - timedelta(days=1), T0,
                                   T0 + timedelta(days=30), TLP.AMBER))
    out = state.helix.process(tx("T1", dst="MULE-9"))
    assert "indicator" in [c.contributor_id for c in out.stage1.contributions]
    assert state.indicators.matches_total == 1


def test_force_case_below_threshold_creates_a_case():
    h = Helix(soc_config(), ManualClock(T0), sensor_clock=lambda: 0.0)
    out = h.process(tx("T1"))
    assert out.case_id is None  # quiet event, no Canaries attached
    h.canaries = CanaryRegistry(accounts=["ACC123456"])
    h.canaries.fraud_mass = 0.95
    # bypass the canary mass: route directly with a below-threshold risk
    ch = h.cases.on_scored_event(tx("T2", dst="ACC123456"), out.stage1, 0.0, force_case=True)
    assert ch is not None and ch.case_id in h.cases.canary_cases


# --- Prioritiser exception (Req 33.4) -------------------------------------------------------


def _prioritiser(budget: int = 1) -> Prioritiser:
    cfg = HelixConfig().prioritiser.model_copy(update={"alert_budget": budget, "investigation_cost": 10.0})
    return Prioritiser(cfg, ManualClock(T0))


def key(i: int, loss: str, canary: bool = False) -> CaseKey:
    return CaseKey(f"C-{i}", i, T0, Decimal(loss), canary=canary)


def test_canary_case_is_admitted_outside_the_budget_and_never_displaced():
    p = _prioritiser(budget=1)
    assert p.upsert(key(1, "100")) is Placement.ACTIVE
    assert p.upsert(key(2, "1", canary=True)) is Placement.ACTIVE  # priority <= 0 and budget used up
    assert p.admitted_now == 1
    assert p.upsert(key(3, "500")) is Placement.ACTIVE  # displaces C-1, not the Canary Case
    assert p.placement_of("C-1") is Placement.BACKLOG and p.placement_of("C-2") is Placement.ACTIVE
    assert p.upsert(key(2, "0", canary=True)) is Placement.ACTIVE


def test_becoming_canary_releases_the_budget_slot():
    p = _prioritiser(budget=1)
    p.upsert(key(1, "100"))
    p.upsert(key(1, "100", canary=True))
    assert p.admitted_now == 0
    assert p.upsert(key(2, "50")) is Placement.ACTIVE


ops = st.lists(st.tuples(st.integers(0, 7), st.integers(-50, 500), st.booleans(), st.booleans()),
               min_size=1, max_size=40)


@settings(max_examples=150, deadline=None)
@given(ops=ops, budget=st.integers(1, 4))
def test_property_canary_cases_always_active_and_uncounted(ops, budget):
    """**Validates: Requirements 33.4, 14.14** Canary Cases are always Active and never count
    against the Alert_Budget; non-Canary admissions stay within it; a Case stays Canary."""
    p = _prioritiser(budget)
    clock = p.clock
    canary: set[str] = set()
    for i, loss, is_canary, tick in ops:
        if tick:
            clock.advance(timedelta(seconds=p.period.total_seconds()))
            p.on_tick(clock.now())
        cid = f"C-{i}"
        if is_canary:
            canary.add(cid)
        p.upsert(CaseKey(cid, i, T0, Decimal(loss), canary=cid in canary))
        for c in canary:
            assert p.placement_of(c) is Placement.ACTIVE
        counted = sum(1 for c in p.active_ids()
                      if c not in canary and p.admitted_period_of(c) == p.current_period)
        assert p.admitted_now <= budget
        assert counted <= p.admitted_now


# --- Audit_Ledger hooks (Req 18.9, 35.1) ---------------------------------------------------


def test_label_escalation_and_weight_changes_are_audited_exactly_once():
    state = soc_state(learner=True, sensors=[Hot()], motif_engine=MotifEngine(detectors=()),
                      weights={"fixed": 0.5})  # below the upper bound, so the learner can raise it
    h, ledger = state.helix, state.ledger
    out = h.process(tx("T1", src="VICTIM", dst="MULE", amount="9999.00", device_id="DEV-Z"))
    h.open_case(out.case_id, "inv")
    h.escalate(out.case_id, "inv")
    h.label(out.case_id, "inv", Label.CONFIRMED_FRAUD, "confirmed")
    cats = categories(ledger)
    assert cats.count("FEEDBACK_LABEL") == 1
    assert cats.count("ESCALATION") == 1
    assert "SENSOR_WEIGHT_CHANGE" in cats  # Feedback_Learner change on CONFIRMED_FRAUD
    lab = next(e for e in ledger.entries() if e.category == "FEEDBACK_LABEL")
    assert lab.actor == "inv" and lab.payload["case_id"] == out.case_id
    assert {"VICTIM", "MULE", "DEV-Z"} <= {i.value for i in state.indicators.all()}
    assert ledger.verify().valid


def test_set_weights_rule_and_config_changes_are_audited():
    state = soc_state()
    h, ledger = state.helix, state.ledger
    h.set_weights({"amount": 0.5})
    h.rules.submit(RULE)
    h.rules.activate("R-BIG", by="ana")
    h.rules.deactivate("R-BIG", by="ana")
    cand = state.cfg.model_copy(update={"prioritiser": state.cfg.prioritiser.model_copy(
        update={"alert_budget": 7})})
    state.apply_runtime_config(cand, {"prioritiser.alert_budget": 7})
    rule_actions = [e.payload["action"] for e in ledger.entries() if e.category == "RULE_CHANGE"]
    assert rule_actions == ["submitted", "activated", "deactivated"]
    assert categories(ledger).count("SENSOR_WEIGHT_CHANGE") == 1
    cfg = [e.payload for e in ledger.entries() if e.category == "CONFIG_CHANGE"]
    assert cfg and cfg[0]["name"] == "prioritiser.alert_budget" and cfg[0]["new"] == 7


# --- Playbook_Runner on Case updates (Req 32.2) ----------------------------------------------


def test_playbooks_run_on_case_updates_including_escalation():
    state = soc_state(sensors=[Hot()], motif_engine=MotifEngine(detectors=()))
    h, runner = state.helix, state.playbook_runner
    out = h.process(tx("T1", amount="9999.00"))
    h.open_case(out.case_id, "inv")
    h.escalate(out.case_id, "inv")  # CRITICAL: the critical playbook triggers
    assert runner.runs(out.case_id)
    assert runner.actions(out.case_id)
    assert "PLAYBOOK_ACTION" in categories(state.ledger)
    n = len(runner.actions(out.case_id))
    h.process(tx("T2", amount="9999.00", ts=T0 + timedelta(seconds=1)))  # Case updated again
    assert len(runner.actions(out.case_id)) == n  # Req 32.8: each version at most once per Case


# --- generator and attacker (Req 33.5, 33.7, 30.5) ----------------------------------------------


@settings(max_examples=10, deadline=None)
@given(seed=st.integers(0, 10_000))
def test_property_generator_never_emits_canaries(seed):
    """**Validates: Requirements 33.5** Legitimate generator events reference no Canary."""
    cfg = soc_config()
    cfg = cfg.model_copy(update={"generator": cfg.generator.model_copy(update={"accounts": 200})})
    reg = CanaryRegistry.generate(seed)
    gen = SyntheticGenerator(cfg.generator, ManualClock(T0), InMemoryGroundTruth(), seed=seed)
    for _ in range(300):
        assert not reg.touches(next(gen))


def test_attacker_population_includes_canaries_and_records_technique_tags():
    state = soc_state()
    gt = InMemoryGroundTruth()
    runner = attach_attacker(state, seed=3, writer=gt)
    agent = runner.agent
    pool = set(agent.targets.accounts)
    assert set(state.canaries.ids(CanaryKind.ACCOUNT)) <= pool
    assert set(state.canaries.ids(CanaryKind.PAYEE)) <= pool
    assert not set(state.canaries.ids(CanaryKind.DEVICE)) & pool
    assert all(agent.genome_techniques(g.genome_id) for g in agent.genomes())
    agent.note_ingested(1_000_000, at=state.clock.now())
    now = state.clock.now()
    for _ in range(240):
        now += timedelta(seconds=30)
        agent.tick(now)
        if gt.records:
            break
    assert gt.records
    for rec in gt.records:
        assert rec.technique_tags == agent.genome_techniques(rec.genome_id)


def test_attacker_without_soc_has_no_canaries_or_tags():
    cfg = HelixConfig()
    state = AppState.build(cfg, ManualClock(T0), learner=False, sensor_clock=lambda: 0.0)
    state.canaries = CanaryRegistry.generate(1)  # present but soc is off: never used
    runner = attach_attacker(state, seed=3, writer=InMemoryGroundTruth())
    assert not set(state.canaries.ids()) & set(runner.agent.targets.accounts)


# --- soc.enabled = false: zero behaviour change -----------------------------------------------


def _run_off(seed: int, attach: bool) -> tuple[list, dict]:
    cfg = HelixConfig()
    cfg = cfg.model_copy(update={"generator": cfg.generator.model_copy(update={"accounts": 60})})
    h = Helix(cfg, ManualClock(T0), sensor_clock=lambda: 0.0)
    extras: dict = {}
    if attach:  # every SOC component present on a soc-off pipeline: all must be ignored
        reg = CanaryRegistry.generate(seed)
        ind = IndicatorStore()
        runner = PlaybookRunner.with_defaults()
        ledger = AuditLedger()
        h.canaries, h.indicators, h.playbooks, h.audit = reg, ind, runner, ledger
        extras = {"reg": reg, "ind": ind, "runner": runner, "ledger": ledger}
    gen = SyntheticGenerator(cfg.generator, ManualClock(T0), InMemoryGroundTruth(), seed=seed)
    evs = [next(gen) for _ in range(80)]
    canary = CanaryRegistry.generate(seed).ids(CanaryKind.PAYEE)[0]
    last = evs[-1]
    evs.append(TransactionEvent(tx_id="TX-CANARY", ts=last.ts, src=last.src, dst=canary,
                                amount=Decimal("10.00"), currency=last.currency))
    if attach:
        extras["ind"].add(Indicator(last.src, IndicatorType.ACCOUNT, 100, "t", T0 - timedelta(days=400),
                                    T0, T0 + timedelta(days=4000), TLP.AMBER))
    outs = [h.process(e) for e in evs]
    for cid in sorted(h.cases.cases)[:1]:
        h.open_case(cid, "inv")
        h.escalate(cid, "inv")
    h.set_weights({"amount": 0.7})
    sig = [(o.event.tx_id, round(o.risk, 12), o.case_id, o.tier, o.applied,
            tuple(c.contributor_id for c in o.stage1.contributions)) for o in outs]
    sig.append(tuple(sorted((c.case_id, c.tier.value, c.placement.value) for c in h.cases.cases.values())))
    return sig, extras


@settings(max_examples=5, deadline=None)
@given(seed=st.integers(0, 1000))
def test_property_soc_disabled_means_zero_behaviour_change(seed):
    """**Validates: Requirements 33.2, 34.3, 32.2, 35.1 (gated by soc.enabled)** With soc.enabled
    false, attached SOC components change no outcome and record nothing."""
    plain, _ = _run_off(seed, attach=False)
    wired, x = _run_off(seed, attach=True)
    assert plain == wired
    assert len(x["ledger"]) == 0 and x["reg"].hit_count == 0
    assert x["ind"].matches_total == 0 and x["runner"].runs() == []


def test_soc_disabled_app_state_has_no_soc_wiring():
    state = AppState.build(HelixConfig(), ManualClock(T0), learner=False, sensor_clock=lambda: 0.0)
    h = state.helix
    assert not h.soc_enabled
    assert h.canaries is None and h.playbooks is None and h.audit is None and h.indicators is None
    assert h.cases.canary_cases == set()
    with pytest.raises(ValueError):
        state.enable_soc()
