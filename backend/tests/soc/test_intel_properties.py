"""Hypothesis properties of the Indicator_Store (Req 34.3, 34.4, 34.5, 34.8).

Design (Increment 4 headline property): *for any* valid Indicator set, export then import SHALL
produce equal Indicators (Req 34.5); plus the module's own properties: the indicator mass
formula, expiry exclusion and TLP:RED exclusion.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.fusion import ContributorType, is_valid
from helix.soc.intel import (
    TLP,
    Indicator,
    IndicatorConfig,
    IndicatorStore,
    IndicatorType,
    parse_bundle,
    to_bundle,
)

pytestmark = pytest.mark.property

FAST = settings(max_examples=60, deadline=None, derandomize=True, database=None)
ZONES = st.sampled_from([timezone.utc, timezone(timedelta(hours=2)), timezone(timedelta(hours=-5, minutes=-30))])
moments = st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=ZONES)
id_text = st.text(st.characters(min_codepoint=32, max_codepoint=0x2FF, blacklist_characters="\x7f")
                  | st.sampled_from("'\\"), min_size=1, max_size=24).filter(lambda s: s == s.strip() and s)


@st.composite
def indicators(draw, t: IndicatorType | None = None):
    t = t or draw(st.sampled_from(list(IndicatorType)))
    value = str(draw(st.ip_addresses())) if t is IndicatorType.IP else draw(id_text)
    first = draw(moments)
    last = first + timedelta(microseconds=draw(st.integers(0, 10**12)))
    expiry = first + timedelta(microseconds=draw(st.integers(1, 10**13)))
    return Indicator(value, t, draw(st.integers(0, 100)), draw(id_text), first, last, expiry,
                     draw(st.sampled_from(list(TLP))))


def unique(inds):
    out = {}
    for i in inds:
        out[i.key] = i
    return sorted(out.values(), key=lambda i: (i.type.value, i.value))


indicator_sets = st.lists(indicators(), max_size=8).map(unique)


@FAST
@given(indicator_sets)
def test_stix_export_then_import_reproduces_the_indicators(inds):
    """**Validates: Requirements 34.5, 34.6**"""
    text = json.dumps(to_bundle(inds, include_red=True))  # through JSON text, as on the wire
    rep = parse_bundle(json.loads(text))
    assert rep.rejected == ()
    assert sorted(rep.imported, key=lambda i: (i.type.value, i.value)) == inds
    st_ = IndicatorStore()
    for i in inds:
        st_.add(i)
    fresh = IndicatorStore()
    fresh.import_bundle(st_.export_bundle(include_red=True))
    assert fresh.all() == st_.all()


@FAST
@given(indicator_sets)
def test_export_excludes_tlp_red_unless_requested(inds):
    """**Validates: Requirements 34.8**"""
    default = parse_bundle(to_bundle(inds)).imported
    assert all(i.tlp is not TLP.RED for i in default)
    assert {i.key for i in default} == {i.key for i in inds if i.tlp is not TLP.RED}
    assert {i.key for i in parse_bundle(to_bundle(inds, include_red=True)).imported} == {i.key for i in inds}


def event_for(i: Indicator, ts: datetime) -> SimpleNamespace:
    attr = {IndicatorType.ACCOUNT: "account_id", IndicatorType.DEVICE: "device_id", IndicatorType.IP: "ip",
            IndicatorType.MERCHANT: "merchant_id"}[i.type]
    return SimpleNamespace(ts=ts, tx_id="T1", **{attr: i.value})


@FAST
@given(indicators(), st.floats(0.0, 1.0, allow_nan=False))
def test_indicator_mass_is_base_times_confidence_with_the_rest_uncertain(i, base):
    """**Validates: Requirements 34.3**"""
    store = IndicatorStore(IndicatorConfig(base_mass=base))
    store.add(i)
    c = store.contribution(event_for(i, i.first_seen))
    assert c is not None and c.kind is ContributorType.INDICATOR
    assert c.mass.f == base * i.confidence / 100 and c.mass.l == 0.0
    assert c.mass.t == pytest.approx(1.0 - c.mass.f, abs=1e-12) and is_valid(c.mass)


@FAST
@given(indicators(), st.integers(-10**13, 10**13))
def test_events_later_than_expiry_never_match(i, offset_us):
    """**Validates: Requirements 34.3, 34.4**"""
    store = IndicatorStore()
    store.add(i)
    ts = i.expiry + timedelta(microseconds=offset_us)
    hit = store.contribution(event_for(i, ts))
    assert (hit is not None) == (ts <= i.expiry)
