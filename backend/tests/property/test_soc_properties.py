"""Property tests for the cyber-fusion layer (Increment 4 headline properties, Req 28, 29)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.cases import CaseManager, KillChain, Prioritiser, apply_multiplier, assign_stage, reconstruct, stage_rank
from helix.codec import AUTH_OPTIONAL_FIELDS, AUTH_REQUIRED_FIELDS, AuthEvent, AuthEventType, TransactionEvent
from helix.codec import format_auth, parse_auth
from helix.config import KILL_CHAIN_ORDER, HelixConfig, KillChainStage, SocConfig
from helix.config.schema import DEFAULT_KILL_CHAIN_MAPPING, PrioritiserConfig
from helix.explain import AUTH_PREFIX, EntryKind, EvidenceChain, EvidenceEntry
from helix.fusion import VACUOUS, Contribution, ContributorType, Mass, fuse
from helix.replay.engine import DefaultStage1Engine
from helix.sensors import AUTH_SIGNALS, AuthHistory, AuthSensor, SensorContext, default_sensors
from helix.sim.clock import ManualClock

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
SOC = SocConfig(enabled=True)

ids = st.text(st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=64)
offsets = st.integers(-23 * 60 - 59, 23 * 60 + 59).map(lambda m: timezone(timedelta(minutes=m)))
aware = st.datetimes(min_value=datetime(1970, 1, 2), max_value=datetime(2100, 1, 1), timezones=offsets)
coord = st.floats(allow_nan=False, allow_infinity=False)


@st.composite
def auth_events(draw) -> AuthEvent:
    kind = draw(st.sampled_from(AuthEventType))
    opt = st.none() | ids
    return AuthEvent(
        event_id=draw(ids), ts=draw(aware), account_id=draw(ids), session_id=draw(ids), event_type=kind,
        device_id=draw(opt), ip=draw(st.none() | st.ip_addresses().map(str)),
        lat=draw(st.none() | st.floats(-90, 90)), lon=draw(st.none() | st.floats(-180, 180)),
        payee_account_id=draw(opt) if kind is AuthEventType.NEW_PAYEE_ADDED else None,
    )


# Feature: helix-fraud-immune-system, Increment 4 property: Auth codec round-trip
@pytest.mark.property
@given(a=auth_events())
def test_auth_codec_round_trip(a: AuthEvent):
    """**Validates: Requirements 28.2, 28.4**"""
    s = format_auth(a)
    doc = json.loads(s)
    assert set(doc) <= set(AUTH_REQUIRED_FIELDS) | set(AUTH_OPTIONAL_FIELDS)
    assert set(AUTH_REQUIRED_FIELDS) <= set(doc)
    back = parse_auth(s)
    assert back == a
    assert format_auth(back) == s


# --- kill chain ---------------------------------------------------------------------------

KEYS = sorted({*DEFAULT_KILL_CHAIN_MAPPING, "sensor.geo", "sensor.velocity", "antibody.AB1", "motif.FAN_IN"})
stages = st.sampled_from(KILL_CHAIN_ORDER)
fmass = st.sampled_from([0.0, 0.0, 0.2, 0.6])


@st.composite
def entries(draw, tx: str) -> EvidenceEntry:
    ts = T0 + timedelta(seconds=draw(st.integers(0, 7_200)))
    kind = draw(st.sampled_from(["sensor", "auth_sensor", "motif", "antibody", "auth_event", "fused", "late"]))
    f = draw(fmass)
    m = Mass(f, 0.0, 1.0 - f)
    if kind == "sensor":
        name = draw(st.sampled_from(["amount", "counterparty", "geo", "velocity", "timing", "drift"]))
        return EvidenceEntry(ts, tx, name, EntryKind.CONTRIBUTOR, "SENSOR", m, draw(st.sampled_from([0.0, 1.0])))
    if kind == "auth_sensor":
        sig = draw(st.lists(st.sampled_from(AUTH_SIGNALS), unique=True))
        return EvidenceEntry(ts, tx, "auth", EntryKind.CONTRIBUTOR, "SENSOR", m, 1.0,
                             features={"signals": "|".join(sig)})
    if kind == "motif":
        mt = draw(st.sampled_from(["FAN_OUT", "FAN_IN", "STRUCTURING", "MULE_CHAIN", "ROUND_TRIP_CYCLE"]))
        return EvidenceEntry(ts, tx, f"M-{mt}", EntryKind.MOTIF, "MOTIF", m, 1.0, features={"motif_type": mt})
    if kind == "antibody":
        return EvidenceEntry(ts, tx, "AB1", EntryKind.CONTRIBUTOR, "ANTIBODY", m, 1.0)
    if kind == "auth_event":
        sig = draw(st.none() | st.sampled_from(AUTH_SIGNALS))
        return EvidenceEntry(ts, tx, AUTH_PREFIX + draw(st.sampled_from("abc")), EntryKind.AUTH_EVENT, "AUTH",
                             detail={"signal": sig})
    if kind == "fused":
        return EvidenceEntry(ts, tx, "~fused", EntryKind.FUSED, None, m)
    return EvidenceEntry(ts, tx, "~late", EntryKind.LATE)


@st.composite
def chains(draw) -> EvidenceChain:
    acc: dict[tuple[str, str], EvidenceEntry] = {}
    for tx in draw(st.lists(st.sampled_from(["T1", "T2", "T3"]), min_size=0, max_size=3, unique=True)):
        for e in draw(st.lists(entries(tx), max_size=8)):
            acc.setdefault(e.key, e)
    return EvidenceChain.of("C-1", acc.values())


mappings = st.one_of(st.just(DEFAULT_KILL_CHAIN_MAPPING), st.dictionaries(st.sampled_from(KEYS), stages))


# Feature: helix-fraud-immune-system, Increment 4 property: Kill-chain maximum
@pytest.mark.property
@given(chain=chains(), mapping=mappings, default=stages)
def test_kill_chain_most_advanced_is_max_over_entries(chain, mapping, default):
    """**Validates: Requirements 29.1, 29.2, 29.3, 29.4, 29.5**"""
    kc = reconstruct(chain, mapping, default)
    per = [assign_stage(e, mapping, default) for e in chain]
    # exactly one stage per entry; unmapped entries get the default and are marked
    assert len(kc.assignments) == len(chain)
    for a in per:
        assert a.mapped == (mapping.get(a.key) is not None)
        if not a.mapped:
            assert a.stage is default
    assert {k for k, a in kc.assignments if not a.mapped} == set(kc.unmapped)
    # the invariant (Req 29.5)
    expect = max((a.stage for a in per), key=stage_rank, default=None)
    assert kc.most_advanced is expect
    # stage order with the earliest timestamp per stage (Req 29.4)
    listed = [s.stage for s in kc.stages]
    assert [stage_rank(s) for s in listed] == sorted({stage_rank(a.stage) for a in per})
    for span in kc.stages:
        assert span.first_ts == min(e.ts for e, a in zip(chain, per) if a.stage is span.stage)
    if listed:
        assert kc.most_advanced is listed[-1]


# --- Kill_Chain_Multiplier ---------------------------------------------------------------

money = st.decimals(min_value=0, max_value=10**9, places=2, allow_nan=False, allow_infinity=False)
multipliers = st.one_of(st.just(1.0), st.floats(min_value=1.0, max_value=100.0))


# Feature: helix-fraud-immune-system, Increment 4 property: Kill_Chain_Multiplier never lowers Case_Priority
@pytest.mark.property
@given(el=money, stage=st.none() | stages, m=multipliers, cost=money)
def test_multiplier_never_lowers_priority(el, stage, m, cost):
    """**Validates: Requirements 29.6, 29.7**"""
    p = Prioritiser(PrioritiserConfig(investigation_cost=float(cost)), ManualClock(T0))
    with_m = apply_multiplier(el, stage, m)
    assert with_m >= el
    assert p.priority_for(with_m) >= p.priority_for(el)
    if stage not in (KillChainStage.CASH_OUT, KillChainStage.LAYERING):
        assert with_m == el


# Feature: helix-fraud-immune-system, Increment 4 property: Kill_Chain_Multiplier never lowers Case_Priority
@pytest.mark.property
@given(f=st.floats(0.6, 0.95), amount=st.decimals(min_value="0.01", max_value="99999.99", places=2),
       stage=stages, m=multipliers)
def test_case_priority_with_multiplier_is_at_least_without(f, amount, stage, m):
    """**Validates: Requirements 29.6, 29.7**"""
    ev = TransactionEvent(tx_id="T1", ts=T0, src="A", dst="B", amount=amount, currency="EUR")
    fa, _ = fuse([Contribution("amount", ContributorType.SENSOR, Mass(f, 0.0, 1.0 - f), 1.0)], 0.95,
                 tx_id="T1")
    cfg = HelixConfig()
    risk = fa.belief + 0.5 * fa.uncertainty
    pri = {}
    for name, mult in (("off", None), ("on", m)):
        clock = ManualClock(T0)
        cm = CaseManager(cfg.risk, Prioritiser(cfg.prioritiser, clock), clock, kill_chain_multiplier=mult)
        cm.on_scored_event(ev, fa, risk)
        [case] = cm.open_cases()
        if mult is not None:
            cm.set_kill_chain(case.case_id, KillChain(case.case_id, (), stage, (), ()))
        pri[name] = case.priority
    assert pri["on"] >= pri["off"]


# --- Auth_Sensor without history ------------------------------------------------------------

accounts = st.sampled_from(["A1", "A2", "A3"])


@st.composite
def tx_events(draw) -> TransactionEvent:
    return TransactionEvent(
        tx_id="TX", ts=T0 + timedelta(seconds=draw(st.integers(0, 86_400))), src=draw(accounts), dst="B",
        amount=draw(st.decimals(min_value="0.01", max_value="99999.99", places=2)), currency="EUR",
        device_id=draw(st.none() | st.sampled_from(["D1", "D2"])),
        session_id=draw(st.none() | st.sampled_from(["S1", "S2"])),
    )


@st.composite
def other_history(draw, exclude: str) -> list[AuthEvent]:
    n = draw(st.integers(0, 12))
    others = [a for a in ("A1", "A2", "A3") if a != exclude]
    return [AuthEvent(event_id=f"E{i}", ts=T0 + timedelta(seconds=draw(st.integers(0, 86_400))),
                      account_id=draw(st.sampled_from(others)), session_id=draw(st.sampled_from(["S1", "S2"])),
                      event_type=draw(st.sampled_from(AuthEventType)), device_id=draw(st.sampled_from(["D1", "D2"])))
            for i in range(n)]


# Feature: helix-fraud-immune-system, Increment 4 property: Auth_Sensor is vacuous without auth history
@pytest.mark.property
@given(data=st.data())
def test_auth_sensor_vacuous_without_account_history(data):
    """**Validates: Requirements 28.8**"""
    ev = data.draw(tx_events())
    h = AuthHistory(SOC)
    for a in data.draw(other_history(ev.src)):
        h.record(a)  # other accounts' telemetry, even on the same session or device ids
    eng = DefaultStage1Engine(sensors=default_sensors())
    out = AuthSensor(h).evaluate(ev, eng.profiles.view(ev.src), SensorContext())
    assert out.mass == VACUOUS and out.vacuous_reason == "history:0"
    assert h.links(ev) == ()
    # and it leaves stage-1 fusion exactly as it is without the Auth_Sensor
    with_auth = DefaultStage1Engine(sensors=[*default_sensors(), AuthSensor(h)], clock=lambda: 0.0)
    without = DefaultStage1Engine(sensors=default_sensors(), clock=lambda: 0.0)
    fa1, _, _ = with_auth.assess(ev)
    fa0, _, _ = without.assess(ev)
    assert fa1.mass == fa0.mass and fa1.conflict == fa0.conflict
