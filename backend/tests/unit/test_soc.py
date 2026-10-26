"""Unit tests for the cyber-fusion layer (Req 28, 29): codec, ingest, auth history, Auth_Sensor,
linkage, kill chain, multiplier, templates, generator and attacker auth traffic."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pytest

from helix.attacker import AttackerAgent, CampaignContext, PatternType, TargetPool, default_genome, generate
from helix.attacker.campaigns import AUTH_VARIANTS
from helix.cases import CaseManager, Prioritiser, apply_multiplier, assign_stage, check_multiplier, reconstruct
from helix.codec import AuthEvent, AuthEventType, CodecError, TransactionEvent, format_auth, parse_auth
from helix.config import ConfigValidationError, HelixConfig, KillChainStage, SocConfig, ensure_valid
from helix.config.schema import AttackerConfig, GeneratorConfig
from helix.core import Helix
from helix.core.pipeline import AuthOutcome
from helix.explain import AUTH_PREFIX, EntryKind, EvidenceChain, EvidenceEntry
from helix.explain.engine import ExplanationEngine
from helix.fusion import VACUOUS, Mass
from helix.ingest import StreamIngestor
from helix.replay.engine import DefaultStage1Engine
from helix.sensors import AuthHistory, AuthSensor, SensorContext, default_sensors
from helix.sim import InMemoryGroundTruth, ManualClock, SyntheticGenerator
from helix.soc import soc_config

T0 = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
SOC = SocConfig(enabled=True)


def auth(eid: str, kind: str | AuthEventType, at: datetime = T0, account: str = "A1", session: str = "S1",
         device: str | None = "D1", **kw) -> AuthEvent:
    return AuthEvent(event_id=eid, ts=at, account_id=account, session_id=session,
                     event_type=AuthEventType(kind), device_id=device, **kw)


def tx(tx_id: str = "T1", at: datetime = T0, src: str = "A1", dst: str = "B1", session: str | None = None,
       device: str | None = None, amount: str = "100.00") -> TransactionEvent:
    return TransactionEvent(tx_id=tx_id, ts=at, src=src, dst=dst, amount=Decimal(amount), currency="EUR",
                            session_id=session, device_id=device)


def doc(**over) -> dict:
    d = {"event_id": "E1", "ts": "2024-06-01T12:00:00Z", "account_id": "A1", "session_id": "S1",
         "event_type": "LOGIN_SUCCESS"}
    d.update(over)
    return d


def evaluate(sensor: AuthSensor, ev: TransactionEvent):
    return sensor.evaluate(ev, DefaultStage1Engine().profiles.view(ev.src), SensorContext())


# --- Auth_Event_Codec (Req 28.2-28.4) ----------------------------------------------------


def test_parse_valid_document_ignores_extra_fields_and_canonicalises_ip():
    ev = parse_auth(json.dumps(doc(ip="2001:DB8::0001", lat=52.5, lon=13, extra="x", attack=True)))
    assert isinstance(ev, AuthEvent)
    assert ev.event_type is AuthEventType.LOGIN_SUCCESS and ev.ip == "2001:db8::1" and ev.lon == 13.0
    assert not hasattr(ev, "extra") and not hasattr(ev, "attack")
    assert parse_auth(format_auth(ev)) == ev
    assert "device_id" not in json.loads(format_auth(ev))  # absent optionals are not written


@pytest.mark.parametrize("field", ["event_id", "ts", "account_id", "session_id", "event_type"])
def test_missing_required_field_is_named(field):
    d = doc()
    del d[field]
    err = parse_auth(json.dumps(d))
    assert isinstance(err, CodecError) and err.fields == {field}


@pytest.mark.parametrize("over,field", [
    ({"event_type": "LOGOUT"}, "event_type"),
    ({"event_type": 3}, "event_type"),
    ({"ts": "2024-06-01T12:00:00"}, "ts"),  # naive
    ({"ts": "yesterday"}, "ts"),
    ({"ts": 1717243200}, "ts"),  # epoch numbers are malformed
    ({"ip": "999.1.1.1"}, "ip"),
    ({"lat": 91}, "lat"),
    ({"payee_account_id": "P1"}, "payee_account_id"),  # only for NEW_PAYEE_ADDED
])
def test_invalid_field_is_named(over, field):
    err = parse_auth(json.dumps(doc(**over)))
    assert isinstance(err, CodecError) and field in err.fields, err


def test_new_payee_may_carry_payee_and_documents_are_objects():
    ev = parse_auth(json.dumps(doc(event_type="NEW_PAYEE_ADDED", payee_account_id="P1")))
    assert isinstance(ev, AuthEvent) and ev.payee_account_id == "P1"
    assert isinstance(parse_auth("[1]"), CodecError) and isinstance(parse_auth("{"), CodecError)
    assert isinstance(parse_auth(json.dumps(doc(lat=float("nan")))), CodecError)


# --- Stream_Ingestor (Req 28.1, 28.3) -----------------------------------------------------


def test_ingestor_admits_rejects_and_dedups_auth_events_without_touching_tx_counters():
    ing = StreamIngestor(clock=ManualClock(T0))
    assert ing.submit_auth(json.dumps(doc())).accepted
    assert ing.submit_auth(json.dumps(doc())).status.value == "duplicate"
    bad = ing.submit_auth(json.dumps(doc(event_type="LOGOUT")))
    assert bad.status.value == "rejected" and bad.kind == "auth"
    [rec] = ing.rejected_log.records()
    assert rec.fields == ("event_type",)
    assert ing.submit(auth("E2", "LOGIN_FAILURE")).accepted  # objects also go through submit
    c = ing.auth_counters
    assert (c.accepted, c.duplicate, c.malformed) == (2, 1, 1)
    assert ing.counters.accepted == ing.counters.malformed == 0


def test_auth_and_tx_of_one_account_leave_in_timestamp_order():
    ing = StreamIngestor(clock=ManualClock(T0))
    ing.submit(tx("T1", T0 + timedelta(seconds=30)))
    ing.submit_auth(auth("E1", "LOGIN_SUCCESS", T0))
    ing.submit_auth(auth("E2", "NEW_PAYEE_ADDED", T0 + timedelta(seconds=10), payee_account_id="B1"))
    out = [a.event for a in ing.drain()]
    assert [getattr(e, "event_id", None) or e.tx_id for e in out] == ["E1", "E2", "T1"]
    assert ing.latest_processed("A1") == T0 + timedelta(seconds=30)  # only transactions move it


# --- AuthHistory and Suspicious_Auth_Activity -------------------------------------------


def test_history_is_bounded_by_count_time_and_accounts():
    h = AuthHistory(SocConfig(enabled=True, auth_history_max_events=10, auth_max_accounts=2,
                              auth_retention_s=3_600, auth_correlation_window_s=1_800))
    for i in range(15):
        h.record(auth(f"E{i}", "LOGIN_SUCCESS", T0 + timedelta(seconds=i)))
    assert len(h.events("A1")) == 10 and h.events("A1")[0].event.event_id == "E5"
    h.record(auth("LATE", "LOGIN_SUCCESS", T0 + timedelta(hours=2)))
    assert [r.event.event_id for r in h.events("A1")] == ["LATE"]  # older than retention dropped
    assert h.known_devices("A1") == ("D1",)  # known devices survive event retention
    later = T0 + timedelta(hours=2)
    h.record(auth("B", "LOGIN_SUCCESS", later, account="A2"))
    h.record(auth("C", "LOGIN_SUCCESS", later, account="A3"))
    assert h.accounts() == ["A2", "A3"]  # least recently recorded account evicted
    assert h.record(auth("C", "LOGIN_SUCCESS", later, account="A3")) is None  # duplicate id
    assert h.duplicates == 1


def test_new_device_needs_a_baseline_and_is_fixed_at_record_time():
    h = AuthHistory(SOC)
    assert not h.record(auth("E1", "LOGIN_SUCCESS", device="D1")).new_device  # first login: no baseline
    assert not h.record(auth("E2", "LOGIN_SUCCESS", device="D1")).new_device
    assert h.record(auth("E3", "LOGIN_SUCCESS", device="D2")).new_device
    assert not h.record(auth("E4", "LOGIN_SUCCESS", device="D2")).new_device
    assert not h.record(auth("E5", "LOGIN_FAILURE", device="D9")).new_device  # failures do not enrol


@pytest.mark.parametrize("n,spread_s,expected", [(4, 60, False), (5, 60, True), (5, 700, False)])
def test_failed_login_burst_threshold_and_span(n, spread_s, expected):
    h = AuthHistory(SOC)
    for i in range(n):
        h.record(auth(f"F{i}", "LOGIN_FAILURE", T0 + timedelta(seconds=i * spread_s)))
    act = h.suspicious("A1", T0 + timedelta(seconds=n * spread_s))
    assert bool(act) is expected
    assert act.failed_logins == (n if expected else 0)


@pytest.mark.parametrize("kind,signal", [("PASSWORD_RESET", "password_reset"), ("MFA_FAILURE", "mfa_failure"),
                                         ("NEW_PAYEE_ADDED", "new_payee")])
def test_single_event_signals(kind, signal):
    h = AuthHistory(SOC)
    h.record(auth("E1", kind, T0))
    assert h.suspicious("A1", T0 + timedelta(minutes=29)).signals == {signal}
    assert not h.suspicious("A1", T0 + timedelta(minutes=31))  # outside the 30 min window
    assert not h.suspicious("A1", T0 - timedelta(seconds=1))  # only events before the payment


# --- linkage (Req 28.7) -------------------------------------------------------------------


def test_links_by_session_or_device_same_account_inside_the_window():
    h = AuthHistory(SOC)
    h.record(auth("BASE", "LOGIN_SUCCESS", T0 - timedelta(days=2), session="S0", device="D1"))
    h.record(auth("SES", "LOGIN_SUCCESS", T0, session="S1", device="D1"))
    h.record(auth("DEV", "MFA_SUCCESS", T0, session="S2", device="D7"))
    h.record(auth("NONE", "MFA_SUCCESS", T0, session="S3", device="D8"))
    h.record(auth("OTHER", "LOGIN_SUCCESS", T0, account="A2", session="S1", device="D7"))
    h.record(auth("OLD", "LOGIN_SUCCESS", T0 - timedelta(hours=1), session="S1", device="D1"))
    links = {l.event.event_id: l for l in h.links(tx(at=T0 + timedelta(minutes=5), session="S1", device="D7"))}
    assert set(links) == {"SES", "DEV"}
    assert links["SES"].linked_by == "session" and links["DEV"].linked_by == "device"
    assert not links["SES"].suspicious


# --- Auth_Sensor (Req 28.8) ---------------------------------------------------------------


def test_auth_sensor_is_vacuous_without_history_and_without_correlation():
    h = AuthHistory(SOC)
    s = AuthSensor(h)
    out = evaluate(s, tx())
    assert out.mass == VACUOUS and out.vacuous_reason == "history:0"
    h.record(auth("E1", "PASSWORD_RESET", T0 - timedelta(hours=2)))
    out = evaluate(s, tx())
    assert out.mass == VACUOUS and out.vacuous_reason is None  # history, nothing in the window


def test_auth_sensor_puts_fraud_mass_on_suspicious_activity_and_names_it():
    h = AuthHistory(SOC)
    s = AuthSensor(h)
    h.record(auth("E0", "LOGIN_SUCCESS", T0 - timedelta(days=1), device="D1"))
    h.record(auth("E1", "LOGIN_SUCCESS", T0, device="D9", session="S9"))
    h.record(auth("E2", "NEW_PAYEE_ADDED", T0 + timedelta(seconds=5), session="S9", payee_account_id="B1"))
    out = evaluate(s, tx(at=T0 + timedelta(minutes=1), session="S9"))
    assert out.mass.f > 0 and out.mass.l == 0
    assert out.features["suspicious_ids"] == "E1|E2" and out.features["signals"] == "new_device|new_payee"
    assert out.features["payee_match"] == 1.0
    # paying the new payee is stronger evidence than paying someone else
    other = evaluate(s, tx("T2", at=T0 + timedelta(minutes=1), dst="ZZ", session="S9"))
    assert 0 < other.mass.f < out.mass.f


def test_auth_sensor_known_device_login_is_mildly_legit():
    h = AuthHistory(SOC)
    h.record(auth("E0", "LOGIN_SUCCESS", T0 - timedelta(days=1), device="D1"))
    h.record(auth("E1", "LOGIN_SUCCESS", T0, device="D1", session="S5"))
    out = evaluate(AuthSensor(h), tx(at=T0 + timedelta(seconds=20), session="S5"))
    assert out.mass.f == 0 and out.mass.l == pytest.approx(0.1)


def test_default_sensors_gain_the_auth_sensor_only_with_the_flag():
    assert [s.name for s in default_sensors(HelixConfig())] == [
        "amount", "velocity", "timing", "geo", "counterparty", "drift"]
    assert default_sensors(soc_config())[-1].name == "auth"


# --- pipeline -----------------------------------------------------------------------------


def test_pipeline_records_auth_events_without_creating_cases():
    h = Helix(soc_config(), ManualClock(T0), sensor_clock=lambda: 0.0)
    o = h.process(auth("E1", "PASSWORD_RESET"))
    assert isinstance(o, AuthOutcome) and o.recorded
    assert h.auth.has_history("A1") and not h.cases.cases and h.seq_no == 0
    assert not h.process(auth("E1", "PASSWORD_RESET")).recorded  # duplicate id
    assert any(s.name == "auth" for s in h.stage1.sensors)  # runs inside stage1.assess


def test_auth_sensor_survives_a_stage1_wrapper_that_runs_sensors_itself():
    """The immune integration pattern: ``helix.stage1`` replaced by a subclass sharing state
    (``__dict__``) whose ``assess`` calls ``run_sensors(self.sensors)`` itself."""
    from helix.fusion import fuse
    from helix.sensors import run_sensors, to_contributions

    class Wrapped(DefaultStage1Engine):
        calls = 0

        def assess(self, ev):
            Wrapped.calls += 1
            dna = self.profiles.view(ev.src)
            results = run_sensors(ev, dna, SensorContext(cpd_state=self.cpd.state(ev.src)), self.sensors,
                                  self.cfg.fusion.sensor_budget_ms, clock=self.clock)
            fa, errors = fuse(to_contributions(results, self.weights), self.cfg.fusion.high_conflict,
                              tx_id=ev.tx_id, stage=1)
            return fa, results, errors

    h = Helix(soc_config(), ManualClock(T0), sensor_clock=lambda: 0.0)
    w = Wrapped.__new__(Wrapped)
    w.__dict__.update(h.stage1.__dict__)
    h.stage1 = w
    h.process(auth("E1", "PASSWORD_RESET", T0, session="S1"))
    o = h.process(tx("T1", T0 + timedelta(minutes=1), session="S1"))
    assert Wrapped.calls == 1
    res = {r.name: r for r in o.sensor_results}
    assert res["auth"].output.mass.f > 0 and res["auth"].output.features["suspicious_ids"] == "E1"
    assert o.stage1.belief > 0


def test_pipeline_flag_off_ignores_auth_events():
    h = Helix(HelixConfig(), ManualClock(T0))
    assert h.auth is None and not h.process(auth("E1", "PASSWORD_RESET")).recorded
    assert h.clock.now() == T0  # stream time unchanged


# --- kill chain (Req 29) ------------------------------------------------------------------


def entry(cid: str, kind: EntryKind, ctype: str | None = None, f: float = 0.0, **kw) -> EvidenceEntry:
    mass = Mass(f, 0.0, 1.0 - f) if kind in (EntryKind.CONTRIBUTOR, EntryKind.MOTIF, EntryKind.FUSED) else None
    weight = 1.0 if kind in (EntryKind.CONTRIBUTOR, EntryKind.MOTIF) else None
    return EvidenceEntry(kw.pop("ts", T0), kw.pop("tx", "T1"), cid, kind, ctype, mass, weight, **kw)


def test_stage_assignment_mapping_and_unmapped_default():
    amount = entry("amount", EntryKind.CONTRIBUTOR, "SENSOR", 0.4)
    quiet = entry("amount", EntryKind.CONTRIBUTOR, "SENSOR", 0.0, tx="T2")
    geo = entry("geo", EntryKind.CONTRIBUTOR, "SENSOR", 0.5)
    sensor = entry("auth", EntryKind.CONTRIBUTOR, "SENSOR", 0.6, features={"signals": "failed_logins|new_payee"})
    motif = entry("M1", EntryKind.MOTIF, "MOTIF", 0.5, features={"motif_type": "FAN_OUT"})
    link = entry(AUTH_PREFIX + "E1", EntryKind.AUTH_EVENT, "AUTH", detail={"signal": None})
    assert assign_stage(amount).stage is KillChainStage.CASH_OUT
    assert assign_stage(quiet).stage is KillChainStage.RECONNAISSANCE  # no FRAUD evidence
    assert assign_stage(sensor).stage is KillChainStage.PAYEE_MANIPULATION  # most advanced signal
    assert assign_stage(motif).stage is KillChainStage.LAYERING
    assert assign_stage(link).stage is KillChainStage.RECONNAISSANCE
    g = assign_stage(geo)
    assert (g.stage, g.mapped, g.key) == (KillChainStage.ACCOUNT_TAKEOVER, False, "sensor.geo")  # Req 29.3
    other = assign_stage(geo, default=KillChainStage.CREDENTIAL_ACCESS)
    assert other.stage is KillChainStage.CREDENTIAL_ACCESS and not other.mapped
    kc = reconstruct(EvidenceChain.of("C-1", [amount, quiet, geo, sensor, motif, link]))
    assert kc.most_advanced is KillChainStage.LAYERING and kc.multiplied
    assert kc.unmapped == (("geo", "T1"),)
    assert reconstruct(EvidenceChain.of("C-2")).most_advanced is None


def test_multiplier_validation_and_application():
    for bad in (0.99, 0, -1, float("nan"), float("inf"), True, "2"):
        with pytest.raises(ValueError):
            check_multiplier(bad)
    assert check_multiplier(1) == 1.0
    assert apply_multiplier(Decimal("100.00"), KillChainStage.CASH_OUT, 1.5) == Decimal("150.00")
    assert apply_multiplier(Decimal("100.00"), KillChainStage.LAYERING, 2) == Decimal("200.00")
    assert apply_multiplier(Decimal("100.00"), KillChainStage.PAYEE_MANIPULATION, 3) == Decimal("100.00")
    assert apply_multiplier(Decimal("100.00"), None, 3) == Decimal("100.00")


def test_multiplier_below_one_is_rejected_by_config_and_case_manager():
    with pytest.raises(ConfigValidationError) as exc:
        ensure_valid(HelixConfig(soc=SocConfig(enabled=True, kill_chain_multiplier=0.5)))
    assert [e.path for e in exc.value.errors] == ["soc.kill_chain_multiplier"]
    clock = ManualClock(T0)
    cm = CaseManager(HelixConfig().risk, Prioritiser(HelixConfig().prioritiser, clock), clock,
                     kill_chain_multiplier=1.5)
    with pytest.raises(ValueError):
        cm.set_kill_chain_multiplier(0.5)
    assert cm.kill_chain_multiplier == 1.5  # previous value kept (Req 29.8)
    assert cm.set_kill_chain_multiplier(2) == 2.0


# --- explanation templates ------------------------------------------------------------------


def test_auth_template_and_auth_event_note():
    sensor = entry("auth", EntryKind.CONTRIBUTOR, "SENSOR", 0.6, features={
        "signals": "password_reset|new_device", "window_s": 1800.0, "suspicious_ids": "E1|E2",
        "new_device": "D9", "linked": 1.0})
    text, generic = ExplanationEngine()._render(sensor)
    assert not generic
    assert text == "password reset; login from new device D9 within 30 min before the payment " \
                   "(suspicious auth events E1|E2)"
    benign = entry("auth", EntryKind.CONTRIBUTOR, "SENSOR", 0.0, features={"signals": "", "window_s": 1800.0,
                                                                           "linked": 2.0})
    assert "known device" in ExplanationEngine()._render(benign)[0]
    from helix.explain.engine import _note

    link = entry(AUTH_PREFIX + "E1", EntryKind.AUTH_EVENT, "AUTH", detail={
        "event_id": "E1", "event_type": "MFA_FAILURE", "signal": "mfa_failure", "linked_by": "session"})
    assert _note(link) == f"T1: auth MFA_FAILURE E1 at {T0.isoformat()} suspicious (mfa_failure) by session"


# --- Synthetic_Generator benign auth traffic (Req 28.5) --------------------------------------


def gen(soc: SocConfig | None) -> SyntheticGenerator:
    return SyntheticGenerator(GeneratorConfig(accounts=30), ManualClock(T0), InMemoryGroundTruth(), seed=5, soc=soc)


def test_generator_emits_no_auth_events_without_the_flag():
    evs = list(gen(None).events_until(T0 + timedelta(minutes=1)))
    assert evs and all(isinstance(e, TransactionEvent) for e in evs)
    assert all(isinstance(e, TransactionEvent) for e in gen(SocConfig()).events_until(T0 + timedelta(minutes=1)))


def test_generator_emits_a_login_before_every_session_and_keeps_tx_sequence():
    g = gen(SocConfig(enabled=True, login_failure_frac=0.5))
    evs = list(g.events_until(T0 + timedelta(minutes=2)))
    txs = [e for e in evs if isinstance(e, TransactionEvent)]
    assert txs == list(gen(None).events_until(T0 + timedelta(minutes=2)))  # same Transaction_Events
    enrol = [e for e in evs if isinstance(e, AuthEvent) and e.session_id.startswith("ENROL-")]
    assert len(enrol) == sum(len(p.devices) for p in g.profiles)
    h = AuthHistory(SocConfig(enabled=True))
    failures = 0
    for i, e in enumerate(evs):
        if isinstance(e, AuthEvent):
            rec = h.record(e)
            if not e.session_id.startswith("ENROL-"):  # enrolments are a day before t0
                assert not rec.new_device  # session logins always use a known device
            failures += e.event_type is AuthEventType.LOGIN_FAILURE
        else:
            login = evs[i - 1]
            assert isinstance(login, AuthEvent) and login.event_type is AuthEventType.LOGIN_SUCCESS
            assert login.account_id == e.src and login.ts < e.ts
            assert e.session_id is None or login.session_id == e.session_id
            assert not h.suspicious(e.src, e.ts)  # benign traffic is never Suspicious_Auth_Activity
    assert failures > 0


# --- Attacker_Agent auth sequences (Req 20.2, 28.6) ------------------------------------------


def pool() -> list[str]:
    return [p.account_id for p in gen(None).profiles]


def ato_ctx(seed: int = 0, **kw) -> CampaignContext:
    return CampaignContext(rng=np.random.default_rng(seed), start=T0, targets=TargetPool(pool()),
                           id_prefix=f"t{seed}", reporting_threshold=Decimal("10000"), max_events=200, **kw)


@pytest.mark.parametrize("variant", AUTH_VARIANTS)
def test_ato_variants_emit_suspicious_auth_before_the_transfers(variant):
    evs = generate(default_genome(PatternType.ACCOUNT_TAKEOVER, "G1"), ato_ctx(auth=True, auth_variant=variant))
    auths = [e for e in evs if isinstance(e, AuthEvent)]
    txs = [e for e in evs if isinstance(e, TransactionEvent)]
    assert auths and txs and [e.ts for e in evs] == sorted(e.ts for e in evs)
    assert max(a.ts for a in auths) < min(t.ts for t in txs)
    victim = {t.src for t in txs}
    assert victim == {a.account_id for a in auths}
    h = AuthHistory(SOC)
    h.record(auth("BASE", "LOGIN_SUCCESS", T0 - timedelta(days=1), account=victim.pop(), device="VICTIM-DEV"))
    for a in auths:
        h.record(a)
    first = txs[0]
    act = h.suspicious(first.src, first.ts)
    assert "new_payee" in act.signals
    assert {"failed_logins", "password_reset", "mfa_failure", "new_device"} & act.signals
    assert auths[-1].payee_account_id == txs[0].dst


def test_ato_transaction_leg_is_unchanged_by_the_auth_leg():
    g = default_genome(PatternType.ACCOUNT_TAKEOVER, "G1")
    plain = generate(g, ato_ctx(3))
    fused = [e for e in generate(g, ato_ctx(3, auth=True)) if isinstance(e, TransactionEvent)]
    assert [(e.tx_id, e.dst, e.amount) for e in plain] == [(e.tx_id, e.dst, e.amount) for e in fused]
    assert all(isinstance(e, TransactionEvent) for e in plain)


class LabelCheckingIngest:
    """Asserts a FRAUD label exists for every event at the moment it is submitted."""

    def __init__(self, gt: InMemoryGroundTruth) -> None:
        self.gt, self.events, self.backpressure = gt, [], False

    def submit(self, ev):
        eid = ev.event_id if isinstance(ev, AuthEvent) else ev.tx_id
        assert any(r.event_id == eid and r.label == "FRAUD" for r in self.gt.records), eid
        self.events.append(ev)


def test_attacker_labels_auth_events_before_injection_and_scores_transactions_only():
    clock, gt = ManualClock(T0), InMemoryGroundTruth()
    ing = LabelCheckingIngest(gt)
    cfg = AttackerConfig(enabled=True, attack_fraction=1.0, launch_interval_s=86_400, detection_window_s=60)
    detected: set[str] = set()
    a = AttackerAgent(cfg, clock, gt, ing, seed=1, account_pool=pool(), is_detected=detected.__contains__,
                      soc=SOC)
    ato = next(g for g in a.active_genomes() if g.ptype is PatternType.ACCOUNT_TAKEOVER)
    a._launched.update(g.genome_id for g in a.active_genomes() if g is not ato)  # force the ATO genome next
    a.tick(T0)  # launch
    [camp] = a.campaigns()
    assert camp.genome_id == ato.genome_id
    detected.update(i for i in camp.event_ids if i.startswith("TX"))  # every transaction "detected"
    for _ in range(240):
        clock.advance(timedelta(seconds=60))
        a.note_ingested(1000, clock.now())
        a.tick(clock.now())
    auths = [e for e in ing.events if isinstance(e, AuthEvent)]
    txs = [e for e in ing.events if isinstance(e, TransactionEvent)]
    assert auths and txs
    assert {e.event_id for e in auths} | {e.tx_id for e in txs} == set(camp.event_ids)
    assert a.injected_total == len(ing.events) and a.injected_tx_total == len(txs)
    [done] = [c for c in a.completed_campaigns() if c.campaign_id == camp.campaign_id]
    assert set(done.injected_ids) == set(camp.event_ids)
    assert done.evasion == 0.0  # undetected Auth_Events are not scored as evasions


def test_attacker_without_flag_emits_no_auth_events():
    evs = generate(default_genome(PatternType.ACCOUNT_TAKEOVER, "G1"), ato_ctx())
    assert evs and all(isinstance(e, TransactionEvent) for e in evs)
