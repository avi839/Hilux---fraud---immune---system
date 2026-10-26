"""Property tests for helix.sensors (design Properties 9-16; the runner parts of
Property 15 belong to task 5.2)."""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from helix.changepoint import ChangePointDetector
from helix.codec import TransactionEvent
from helix.config.schema import HelixConfig, SensorConfig
from helix.fusion import VACUOUS, is_valid
from helix.memory import ProfileStore
from helix.sensors import (
    AmountSensor,
    CounterpartySensor,
    GeoSensor,
    SensorContext,
    TimingSensor,
    default_sensors,
    geo_mass,
    haversine_km,
    implied_speed_kmh,
    velocity_mass,
)

pytestmark = pytest.mark.property

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
MAX_S = 30 * 86_400

amounts = st.integers(1, 99_999_999_999).map(lambda c: Decimal(c) / 100)
lats = st.floats(-90, 90, allow_nan=False)
lons = st.floats(-180, 180, allow_nan=False)
locs = st.one_of(st.none(), st.tuples(lats, lons))
dsts = st.sampled_from(["M0", "M1", "M2", "M3", "NEW"])


@st.composite
def events(draw, i: int, t_ref: datetime):
    loc = draw(locs)
    return TransactionEvent.model_construct(
        tx_id=f"T{i}",
        ts=t_ref + timedelta(seconds=draw(st.integers(-3_600, 3 * 86_400))),
        src="A",
        dst=draw(dsts),
        amount=draw(amounts),
        currency="EUR",
        channel=draw(st.sampled_from([None, "card", "online"])),
        lat=None if loc is None else loc[0],
        lon=None if loc is None else loc[1],
    )


@st.composite
def scenarios(draw, min_count: int = 0):
    """(history events, current event, pre-update DNAView, SensorContext)."""
    n = draw(st.integers(min_count, 14))
    hist, t = [], T0
    for i in range(n):
        e = draw(events(i, t))
        hist.append(e)
        t = max(t, e.ts)
    cur = draw(events(999, t))
    store, det = ProfileStore(), ChangePointDetector()
    for e in hist:
        store.apply(e)
        det.observe(e)
    return hist, cur, store.view("A"), SensorContext(det.state("A"))


def _cfg(min_history: int) -> HelixConfig:
    return HelixConfig(sensors=SensorConfig(min_history_count=min_history))


# Feature: helix-fraud-immune-system, Property 9: Contributor output validity
@given(scenarios(), st.integers(1, 10))
def test_p9_sensor_outputs_are_valid_mass_functions(sc, mh):
    """**Validates: Requirements 5.13, 5.14**"""
    _, cur, dna, ctx = sc
    for s in default_sensors(_cfg(mh)):
        out = s.evaluate(cur, dna, ctx)
        assert is_valid(out.mass), (s.name, out)


# Feature: helix-fraud-immune-system, Property 10: Amount monotonicity
@given(scenarios(min_count=5), amounts, amounts)
def test_p10_amount_monotone_in_z(sc, x1, x2):
    """**Validates: Requirements 5.3**"""
    _, cur, dna, ctx = sc
    s = AmountSensor(SensorConfig(min_history_count=5))
    o1 = s.evaluate(cur.model_copy(update={"amount": x1}), dna, ctx)
    o2 = s.evaluate(cur.model_copy(update={"amount": x2}), dna, ctx)
    if o1.features["z"] > o2.features["z"]:
        o1, o2 = o2, o1
    assert o1.mass.f <= o2.mass.f


# Feature: helix-fraud-immune-system, Property 11: Velocity monotonicity
@given(
    st.lists(st.integers(1, 10_000), min_size=3, max_size=3),
    st.lists(st.floats(1.0, 1e6, allow_nan=False), min_size=3, max_size=3),
    st.integers(0, 2),
    st.integers(1, 10_000),
)
def test_p11_velocity_monotone_in_each_count(counts, expected, i, inc):
    """**Validates: Requirements 5.4**"""
    up = list(counts)
    up[i] += inc
    assert velocity_mass(counts, expected)[0] <= velocity_mass(up, expected)[0]


# Feature: helix-fraud-immune-system, Property 12: Timing monotonicity
@given(
    scenarios(min_count=5),
    st.lists(st.floats(0, 100, allow_nan=False), min_size=24, max_size=24),
    st.lists(st.floats(0, 100, allow_nan=False), min_size=7, max_size=7),
    st.floats(0, 1),
    st.booleans(),
)
def test_p12_timing_lower_frequency_never_lowers_fraud(sc, hh, wh, factor, lower_hour):
    """**Validates: Requirements 5.5**"""
    _, cur, dna, ctx = sc
    utc = cur.ts.astimezone(timezone.utc)
    h, w = utc.hour, utc.weekday()
    # Keep both totals positive even after the lowered slot reaches 0 (else the DNA
    # feature is missing and the sensor is vacuous by design).
    hh[(h + 1) % 24] += 1.0
    wh[(w + 1) % 7] += 1.0
    lo_h, lo_w = list(hh), list(wh)
    if lower_hour:
        lo_h[h] *= factor
    else:
        lo_w[w] *= factor
    s = TimingSensor(SensorConfig(min_history_count=5))
    base = s.evaluate(cur, replace(dna, hour_hist=tuple(hh), weekday_hist=tuple(wh)), ctx)
    low = s.evaluate(cur, replace(dna, hour_hist=tuple(lo_h), weekday_hist=tuple(lo_w)), ctx)
    assert base.vacuous_reason is None and low.vacuous_reason is None
    assert base.mass.f <= low.mass.f + 1e-15


# Feature: helix-fraud-immune-system, Property 13: Geo speed and monotonicity
@given(
    scenarios(min_count=5),
    st.tuples(lats, lons),
    st.tuples(lats, lons),
    st.integers(-86_400, 86_400),
    st.integers(0, 86_400),
)
def test_p13_geo_speed_zero_below_vmax_monotone_above(sc, p0, p1, dt_s, extra_s):
    """**Validates: Requirements 5.6, 5.7, 5.8**"""
    _, cur, dna, ctx = sc
    s = GeoSensor(SensorConfig(min_history_count=5))
    t_prev = cur.ts - timedelta(seconds=dt_s)
    d = replace(dna, last_location=(p0[0], p0[1], t_prev))
    ev = cur.model_copy(update={"lat": p1[0], "lon": p1[1]})
    out = s.evaluate(ev, d, ctx)
    dist = haversine_km(p0[0], p0[1], p1[0], p1[1])
    v = implied_speed_kmh(dist, abs(dt_s) / 3600.0)
    assert out.features["speed_kmh"] == v
    if dist > 0 and dt_s == 0:
        assert v == math.inf
    elif dist > 0:
        assert v == pytest.approx(dist / (abs(dt_s) / 3600.0))
    if v <= s.vmax:
        assert out.mass.f == 0.0
    # Same pair further apart in time: speed can only drop, so FRAUD mass can only drop.
    slower_prev = t_prev - timedelta(seconds=extra_s) if dt_s >= 0 else t_prev + timedelta(seconds=extra_s)
    slow = s.evaluate(ev, replace(d, last_location=(p0[0], p0[1], slower_prev)), ctx)
    assert slow.features["speed_kmh"] <= v
    assert slow.mass.f <= out.mass.f


# Feature: helix-fraud-immune-system, Property 13: Geo speed and monotonicity
@given(st.floats(0, 1e7, allow_nan=False), st.floats(0, 1e7, allow_nan=False) | st.just(math.inf))
def test_p13_geo_mass_monotone_in_speed(v1, v2):
    """**Validates: Requirements 5.7**"""
    lo, hi = sorted((v1, v2))
    assert geo_mass(lo, 900.0)[0] <= geo_mass(hi, 900.0)[0]


# Feature: helix-fraud-immune-system, Property 14: Counterparty novelty
@given(scenarios(min_count=5))
def test_p14_unknown_counterparty_strictly_more_fraud(sc):
    """**Validates: Requirements 5.10**"""
    _, cur, dna, ctx = sc
    s = CounterpartySensor(SensorConfig(min_history_count=5))
    known = s.evaluate(cur, replace(dna, counterparties=dna.counterparties | {cur.dst}), ctx)
    unknown = s.evaluate(cur, replace(dna, counterparties=dna.counterparties - {cur.dst}), ctx)
    assert unknown.mass.f > known.mass.f


# Feature: helix-fraud-immune-system, Property 15: Sensor independence (sensor side;
# failure substitution is the runner's, task 5.2)
@given(scenarios(), st.permutations(range(6)), st.integers(1, 10))
def test_p15_outputs_independent_of_invocation_order(sc, perm, mh):
    """**Validates: Requirements 5.2**"""
    _, cur, dna, ctx = sc
    sensors = default_sensors(_cfg(mh))
    alone = {s.name: s.evaluate(cur, dna, ctx) for s in sensors}
    ordered = {}
    for i in perm:
        ordered[sensors[i].name] = sensors[i].evaluate(cur, dna, ctx)
    assert ordered == alone


SENSOR_FIELDS = [
    ("amount", "amount", "missing:amount"),
    ("velocity", "ts", "missing:ts"),
    ("timing", "ts", "missing:ts"),
    ("geo", "lat", "missing:lat,lon"),
    ("geo", "lon", "missing:lat,lon"),
    ("geo", "ts", "missing:ts"),
    ("counterparty", "dst", "missing:dst"),
]


# Feature: helix-fraud-immune-system, Property 16: Missing data or short history is vacuous
@given(scenarios(min_count=5), st.sampled_from(SENSOR_FIELDS), st.sampled_from([None, ""]))
def test_p16_missing_event_field_is_vacuous(sc, case, blank):
    """**Validates: Requirements 7.1, 7.2**"""
    _, cur, dna, ctx = sc
    name, fld, reason = case
    ev = cur.model_copy(update={fld: blank})
    if name == "geo" and fld == "ts":
        assume(ev.lat is not None and ev.lon is not None)
    s = {x.name: x for x in default_sensors(_cfg(5))}[name]
    out = s.evaluate(ev, dna, ctx)
    assert out.mass == VACUOUS and out.vacuous_reason == reason


# Feature: helix-fraud-immune-system, Property 16: Missing data or short history is vacuous
@given(scenarios(), st.integers(1, 15))
def test_p16_short_history_is_vacuous_and_geo_uncertain(sc, mh):
    """**Validates: Requirements 5.15, 7.3, 7.4, 7.7**"""
    _, cur, dna, ctx = sc
    assume(dna.count < mh)
    for s in default_sensors(_cfg(mh)):
        out = s.evaluate(cur, dna, ctx)
        if s.name == "geo":
            assert out.mass.t >= 0.5
        else:
            assert out.mass == VACUOUS and out.vacuous_reason == f"history:{dna.count}", s.name


# Feature: helix-fraud-immune-system, Property 16: Missing data or short history is vacuous
@given(scenarios(min_count=5))
def test_p16_no_prior_location_is_vacuous(sc):
    """**Validates: Requirements 5.9, 7.3**"""
    _, cur, dna, ctx = sc
    ev = cur.model_copy(update={"lat": 10.0, "lon": 10.0})
    out = GeoSensor().evaluate(ev, replace(dna, last_location=None), ctx)
    assert out.mass == VACUOUS and out.vacuous_reason == "missing:dna.last_location"
