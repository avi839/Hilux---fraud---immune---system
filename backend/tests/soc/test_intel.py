"""Unit tests for the threat-intel Indicator_Store (helix.soc.intel, Req 34)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import duckdb
import pytest

from helix.cases import Label
from helix.codec import AuthEvent, TransactionEvent
from helix.fusion import ContributorType, is_valid
from helix.soc.intel import (
    TLP,
    TLP_MARKING_IDS,
    BundleError,
    Indicator,
    IndicatorConfig,
    IndicatorError,
    IndicatorStore,
    IndicatorType,
    attach_indicators,
    event_keys,
    indicator_mass,
    parse_bundle,
    parse_pattern,
    stix_pattern,
    to_bundle,
)

T0 = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
A, D, I, M = IndicatorType.ACCOUNT, IndicatorType.DEVICE, IndicatorType.IP, IndicatorType.MERCHANT


def tx(i: int, src: str = "A", dst: str = "B", minutes: float = 0, **opt) -> TransactionEvent:
    return TransactionEvent(tx_id=f"T{i}", ts=T0 + timedelta(minutes=minutes), src=src, dst=dst,
                            amount=Decimal("10.00"), currency="EUR", **opt)


def auth(i: int, account: str = "A", minutes: float = 0, **opt) -> AuthEvent:
    return AuthEvent(event_id=f"E{i}", ts=T0 + timedelta(minutes=minutes), account_id=account,
                     session_id="S1", event_type="LOGIN_SUCCESS", **opt)


def ind(value: str = "MULE-1", t: IndicatorType = A, conf: int = 75, tlp: TLP = TLP.AMBER,
        days: float = 30) -> Indicator:
    return Indicator(value, t, conf, "test", T0, T0, T0 + timedelta(days=days), tlp)


def case(cid: str = "C-1", accounts=("A", "MULE"), events=()) -> SimpleNamespace:
    return SimpleNamespace(case_id=cid, accounts=set(accounts), tx_ids={e.tx_id for e in events},
                           events=list(events))


# --- Indicator ---------------------------------------------------------------------------------


def test_indicator_validates_every_field_and_normalises_to_utc():
    plus2 = timezone(timedelta(hours=2))
    i = Indicator("D-1", "DEVICE", 50, "s", T0.astimezone(plus2), T0, T0 + timedelta(days=1), "TLP:GREEN")
    assert i.type is D and i.tlp is TLP.GREEN and i.first_seen.tzinfo is timezone.utc
    with pytest.raises(IndicatorError) as e:
        Indicator("", "PHONE", 101, "", datetime(2024, 1, 1), T0, T0, "TLP:PINK")
    fields = {f for f, _ in e.value.problems}
    assert fields == {"type", "confidence", "source", "first_seen", "tlp"}
    with pytest.raises(IndicatorError, match="expiry"):
        Indicator("x", A, 50, "s", T0, T0, T0, TLP.AMBER)
    with pytest.raises(IndicatorError, match="last_seen"):
        Indicator("x", A, 50, "s", T0, T0 - timedelta(seconds=1), T0 + timedelta(days=1), TLP.AMBER)
    with pytest.raises(IndicatorError, match="canonical"):
        ind("10.0.0.01", I)
    with pytest.raises(IndicatorError, match="bool|integer"):
        Indicator("x", A, True, "s", T0, T0, T0 + timedelta(days=1), TLP.AMBER)


def test_config_defaults_and_ranges():
    c = IndicatorConfig()
    assert (c.default_confidence, c.default_expiry_days, c.default_tlp, c.base_mass) == (75, 30.0, TLP.AMBER, 0.8)
    for bad in ({"default_confidence": 101}, {"default_expiry_days": 0}, {"base_mass": 1.5}):
        with pytest.raises(ValueError):
            IndicatorConfig(**bad)


# --- extraction (Req 34.2) -----------------------------------------------------------------------


def test_confirmed_fraud_extracts_accounts_devices_ips_and_merchants_with_defaults():
    st = IndicatorStore(clock=lambda: T0)
    evs = [tx(1, src="A", dst="SHOP", mcc="5411", device_id="DEV-9"), tx(2, src="A", dst="MULE")]
    out = st.on_label(case(events=evs), Label.CONFIRMED_FRAUD,
                      events=[*evs, SimpleNamespace(ip="2001:DB8::1", device_id=None)])
    assert sorted(i.key for i in out) == sorted([(A, "A"), (A, "MULE"), (D, "DEV-9"), (I, "2001:db8::1"),
                                                 (M, "SHOP")])
    for i in out:
        assert (i.confidence, i.tlp, i.source) == (75, TLP.AMBER, "helix:C-1")
        assert i.first_seen == i.last_seen == T0 and i.expiry == T0 + timedelta(days=30)
    assert st.get(M, "SHOP") is not None and st.get(A, "5411") is None  # the MCC itself is no Indicator


def test_other_labels_extract_nothing_and_existing_indicators_only_move_last_seen():
    st = IndicatorStore(clock=lambda: T0)
    assert st.on_label(case(), Label.FALSE_POSITIVE) == [] and len(st) == 0
    st.add(Indicator("MULE", A, 90, "feed", T0 - timedelta(days=5), T0 - timedelta(days=5),
                     T0 + timedelta(days=3), TLP.RED))
    later = T0 + timedelta(days=1)
    st.on_label(case(accounts=("MULE",)), "CONFIRMED_FRAUD", now=later)
    i = st.get(A, "MULE")
    assert i.last_seen == later and i.first_seen == T0 - timedelta(days=5)
    assert (i.confidence, i.tlp, i.expiry, i.source) == (90, TLP.RED, T0 + timedelta(days=3), "feed")


def test_resolver_supplies_events_for_cases_without_them():
    evs = {"T1": tx(1, device_id="DEV-1")}
    st = IndicatorStore(clock=lambda: T0, resolver=evs.get)
    c = SimpleNamespace(case_id="C-2", accounts={"A"}, tx_ids={"T1", "T-unknown"})
    st.on_label(c, Label.CONFIRMED_FRAUD)
    assert st.get(D, "DEV-1") is not None


# --- matching (Req 34.3, 34.4) ---------------------------------------------------------------------


def test_event_keys_cover_transaction_and_auth_fields():
    assert event_keys(tx(1, src="A", dst="SHOP", mcc="5411", device_id="D1")) == [
        (A, "A"), (A, "SHOP"), (D, "D1"), (M, "SHOP")]
    assert event_keys(auth(1, account="A", device_id="D1", ip="192.0.2.7")) == [(A, "A"), (D, "D1"), (I, "192.0.2.7")]
    assert event_keys(SimpleNamespace(ip="not-an-ip", merchant_id="M1")) == [(M, "M1")]


def test_match_emits_base_mass_times_confidence_with_rest_uncertain():
    st = IndicatorStore()
    st.add(ind("DEV-1", D, conf=60))
    c = st.contribution(tx(1, device_id="DEV-1"))
    assert c is not None and c.kind is ContributorType.INDICATOR and c.contributor_id == "indicator"
    assert c.mass.f == pytest.approx(0.8 * 0.6) and c.mass.l == 0.0 and c.mass.t == pytest.approx(1 - 0.48)
    assert is_valid(c.mass) and c.weight == 1.0
    assert st.contribution(tx(2, device_id="DEV-2")) is None
    st2 = IndicatorStore(IndicatorConfig(base_mass=0.5), weights={"indicator": 0.4})
    st2.add(ind("192.0.2.7", I, conf=100))
    c2 = st2.contribution(auth(1, ip="192.0.2.7"))
    assert c2.mass.f == pytest.approx(0.5) and c2.weight == 0.4


def test_best_matching_indicator_wins_and_expired_ones_are_excluded():
    st = IndicatorStore()
    st.add(ind("A", A, conf=40))
    st.add(ind("D1", D, conf=90, days=1))
    ev = tx(1, src="A", device_id="D1", minutes=60)
    assert [i.value for i in st.matches(ev)] == ["D1", "A"]
    assert st.contribution(ev).mass.f == pytest.approx(0.72)
    at_expiry = tx(2, src="A", device_id="D1", minutes=24 * 60)  # ts == expiry: still matches
    assert [i.value for i in st.matches(at_expiry)] == ["D1", "A"]
    after = tx(3, src="A", device_id="D1", minutes=24 * 60 + 1)  # later than expiry: excluded
    assert [i.value for i in st.matches(after)] == ["A"]
    assert st.matches(SimpleNamespace(src="A", ts=None)) == []


def test_extend_stage1_fuses_the_indicator_mass_and_records_the_match():
    from helix.fusion import Contribution, Mass, fuse

    st = IndicatorStore()
    base, _ = fuse([Contribution("amount", ContributorType.SENSOR, Mass(0.2, 0.3, 0.5), 1.0)], 0.95,
                   tx_id="T1", stage=1)
    same, res = st.extend_stage1(tx(1), base, [])
    assert same is base and res == []
    st.add(ind("A", A, conf=100))
    fa, res = st.extend_stage1(tx(1), base, [])
    assert fa.belief > base.belief and [c.contributor_id for c in fa.contributions] == ["amount", "indicator"]
    [r] = res
    assert r.output.features["value"] == "A" and r.output.features["confidence"] == 100.0


# --- STIX bundles (Req 34.5-34.8) -------------------------------------------------------------------


def test_export_shape_and_tlp_red_excluded_by_default():
    st = IndicatorStore()
    st.add(ind("A", A))
    st.add(ind("O'Brien\\x", M, tlp=TLP.RED))
    b = st.export_bundle()
    assert b["type"] == "bundle" and b["id"].startswith("bundle--")
    [mark, obj] = b["objects"]
    assert mark["id"] == TLP_MARKING_IDS[TLP.AMBER] and mark["definition"] == {"tlp": "amber"}
    assert obj["type"] == "indicator" and obj["spec_version"] == "2.1" and obj["pattern_type"] == "stix"
    assert obj["pattern"] == "[user-account:user_id = 'A']" and obj["confidence"] == 75
    assert obj["valid_from"] == "2024-06-01T12:00:00.000000Z" and obj["object_marking_refs"] == [mark["id"]]
    full = st.export_bundle(include_red=True)
    pats = [o["pattern"] for o in full["objects"] if o["type"] == "indicator"]
    assert pats == ["[user-account:user_id = 'A']", "[x-helix-merchant:merchant_id = 'O\\'Brien\\\\x']"]
    assert parse_pattern(pats[1]) == (M, "O'Brien\\x")


def test_export_import_round_trip_into_a_fresh_store():
    st = IndicatorStore()
    for i in (ind("A", A), ind("D", D, conf=0), ind("::1", I, tlp=TLP.WHITE), ind("10.0.0.1", I, tlp=TLP.GREEN),
              ind("M", M, conf=100, tlp=TLP.RED)):
        st.add(i)
    other = IndicatorStore()
    rep = other.import_bundle(st.export_bundle(include_red=True))
    assert rep.rejected == () and rep.skipped == 4 and other.all() == st.all()
    assert stix_pattern(ind("::1", I)) == "[ipv6-addr:value = '::1']"


def test_import_rejects_each_invalid_object_with_every_invalid_field():
    good = to_bundle([ind("A", A), ind("B", A)])
    doc = copy.deepcopy(good)
    objs = [o for o in doc["objects"] if o["type"] == "indicator"]
    bad = objs[1]
    bad["confidence"] = 150
    bad["pattern"] = "[file:name = 'x']"
    del bad["valid_until"]
    bad["object_marking_refs"] = ["marking-definition--00000000-0000-0000-0000-000000000000"]
    doc["objects"].append({"type": "indicator", "id": "nonsense"})
    st = IndicatorStore()
    rep = st.import_bundle(doc)
    assert [i.value for i in rep.imported] == ["A"] and st.get(A, "B") is None
    r1, r2 = rep.rejected
    assert r1.object_id == bad["id"]
    assert {f for f, _ in r1.errors} == {"confidence", "pattern", "valid_until", "object_marking_refs"}
    assert r2.object_id == "nonsense"
    assert {f for f, _ in r2.errors} >= {"id", "spec_version", "pattern_type", "pattern", "created", "valid_from",
                                         "valid_until", "modified", "confidence", "object_marking_refs"}
    assert rep.to_dict()["rejected"][0]["errors"][0].keys() == {"field", "reason"}


def test_import_accepts_bundle_defined_tlp_markings_and_rejects_non_bundles():
    b = to_bundle([ind("A", A)])
    obj = b["objects"][1]
    obj["object_marking_refs"] = ["marking-definition--11111111-2222-3333-4444-555555555555"]
    obj.pop("x_helix_source")
    b["objects"][0] = {"type": "marking-definition", "id": obj["object_marking_refs"][0],
                       "definition_type": "tlp", "definition": {"tlp": "green"}}
    [i] = parse_bundle(b).imported
    assert i.tlp is TLP.GREEN and i.source == "stix-import"
    for doc in ({"type": "not-bundle"}, [], "{", {"type": "bundle", "objects": {}}):
        with pytest.raises(BundleError):
            parse_bundle(doc)


# --- persistence and wiring ---------------------------------------------------------------------------


def test_duckdb_persistence_round_trip():
    con = duckdb.connect(":memory:")
    st = IndicatorStore(clock=lambda: T0, storage=con)
    st.on_label(case(events=[tx(1, device_id="D1")]), Label.CONFIRMED_FRAUD)
    st.add(ind("X", M, tlp=TLP.RED))
    again = IndicatorStore(storage=con)
    assert again.all() == st.all() and len(again) == 4


def test_attach_indicators_wires_the_feedback_hook_into_a_real_pipeline():
    from helix.config import HelixConfig
    from helix.core import Helix
    from helix.fusion import Mass
    from helix.motifs import MotifEngine
    from helix.sensors import SensorOutput
    from helix.sim.clock import ManualClock

    class Hot:
        name = "fixed"

        def evaluate(self, ev, dna_before, ctx):
            return SensorOutput(Mass(0.9, 0.0, 0.1), {})

    h = Helix(HelixConfig(), ManualClock(T0), sensors=[Hot()], sensor_clock=lambda: 0.0,
              motif_engine=MotifEngine(detectors=()))
    st = attach_indicators(h, IndicatorStore())
    attach_indicators(h, st)  # idempotent
    assert h.indicators is st and sum(getattr(f, "indicator_store", None) is st for f in h.feedback_hooks) == 1
    o = h.process(tx(1, src="VICTIM", dst="MULE", device_id="DEV-X"))
    h.open_case(o.case_id, "inv")
    h.label(o.case_id, "inv", Label.CONFIRMED_FRAUD, "")
    assert {i.key for i in st.all()} == {(A, "VICTIM"), (A, "MULE"), (D, "DEV-X")}
    assert all(i.first_seen == T0 for i in st.all())  # stream time of the label
    later = tx(2, src="OTHER", dst="Z", device_id="DEV-X", minutes=5)
    assert st.contribution(later).mass.f == pytest.approx(0.6)
