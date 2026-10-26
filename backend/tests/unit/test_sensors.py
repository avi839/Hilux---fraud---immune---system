"""Unit tests for helix.sensors (design §4, Req 5.3-5.11, 5.15, 7.1-7.4)."""
from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from helix.changepoint import ChangePointDetector, CPDKind
from helix.codec import TransactionEvent
from helix.config.schema import SensorConfig
from helix.fusion import VACUOUS, is_valid
from helix.memory import ProfileStore
from helix.sensors import (
    AmountSensor,
    CounterpartySensor,
    DriftSensor,
    GeoSensor,
    SensorContext,
    SensorParams,
    TimingSensor,
    VelocitySensor,
    default_sensors,
    haversine_km,
    implied_speed_kmh,
)

SAMPLE = Path(__file__).resolve().parents[3] / "docs" / "sample_transactions.jsonl"
T0 = datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)
CTX = SensorContext()


def _sample() -> dict[str, TransactionEvent]:
    out: dict[str, TransactionEvent] = {}
    for line in SAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            if d["tx_id"] in {f"T00{i}" for i in range(1, 8)}:
                out.setdefault(d["tx_id"], TransactionEvent.model_validate(d))
    return out


def _ev(i: int = 0, **kw) -> TransactionEvent:
    base = dict(
        tx_id=f"X{i}", ts=T0 + timedelta(hours=i), src="A", dst="M1",
        amount=Decimal("50.00"), currency="EUR", channel="card", lat=52.52, lon=13.40,
    )
    base.update(kw)
    return TransactionEvent.model_construct(**base)


def _store(events) -> ProfileStore:
    s = ProfileStore()
    for e in events:
        s.apply(e)
    return s


# ---- sample scenario (Alice T001-T007) ----------------------------------------------
def test_alice_t007_amount_geo_counterparty_lean_fraud():
    ev = _sample()
    store = _store(ev[f"T00{i}"] for i in range(1, 7))
    dna = store.view("ACC-ALICE")
    assert dna.count == 6
    sensors = {s.name: s for s in default_sensors()}
    out = {n: s.evaluate(ev["T007"], dna, CTX) for n, s in sensors.items()}
    for n in ("amount", "geo", "counterparty"):
        m = out[n].mass
        assert is_valid(m) and out[n].vacuous_reason is None, n
        assert m.f >= m.l + 0.3, (n, m)
    assert out["geo"].features["speed_kmh"] > 900
    assert 60 < out["geo"].features["dt_h"] * 60 < 80  # 70 min after T005 (T006 has no location)


def test_alice_normal_purchase_leans_legit():
    ev = _sample()
    store = _store(ev[f"T00{i}"] for i in range(1, 6))
    dna = store.view("ACC-ALICE")
    sensors = {s.name: s for s in default_sensors()}
    t006 = ev["T006"]
    a = sensors["amount"].evaluate(t006, dna, CTX).mass
    c = sensors["counterparty"].evaluate(t006, dna, CTX).mass
    assert a.f == 0.0 and a.l > 0.0
    assert c.f == 0.0 and c.l > 0.0
    assert sensors["geo"].evaluate(t006, dna, CTX).vacuous_reason == "missing:lat,lon"


# ---- formulas -----------------------------------------------------------------------
def test_haversine_known_distances():
    assert haversine_km(0, 0, 0, 0) == 0.0
    assert haversine_km(0, 0, 90, 0) == pytest.approx(math.pi * 6371.0088 / 2, rel=1e-12)
    assert haversine_km(52.52, 13.40, 6.52, 3.38) == pytest.approx(5200, rel=0.05)


def test_implied_speed_edge_cases():
    assert implied_speed_kmh(0.0, 0.0) == 0.0
    assert implied_speed_kmh(10.0, 0.0) == math.inf
    assert implied_speed_kmh(100.0, -2.0) == 50.0


def test_geo_same_timestamp_far_away_is_max_fraud():
    s = GeoSensor()
    hist = [_ev(i) for i in range(6)]
    dna = _store(hist).view("A")
    cur = _ev(99, ts=hist[-1].ts, lat=40.7, lon=-74.0)
    out = s.evaluate(cur, dna, CTX)
    assert out.features["speed_kmh"] == math.inf
    assert out.mass.f == pytest.approx(SensorParams().geo_a)


def test_velocity_window_counts_include_current_and_exclude_open_bound():
    t = T0 + timedelta(days=1)
    recent = (t - timedelta(hours=24), t - timedelta(minutes=59), t - timedelta(seconds=30), t)
    assert VelocitySensor.window_counts(t, recent) == (3, 4, 4)


def test_velocity_burst_raises_fraud():
    hist = [_ev(i, ts=T0 + timedelta(hours=6 * i)) for i in range(8)]
    burst = [_ev(100 + j, ts=hist[-1].ts + timedelta(seconds=10 * (j + 1))) for j in range(6)]
    s = VelocitySensor()
    calm = s.evaluate(_ev(50, ts=hist[-1].ts + timedelta(hours=6)), _store(hist).view("A"), CTX)
    hot = s.evaluate(_ev(51, ts=burst[-1].ts + timedelta(seconds=5)), _store(hist + burst).view("A"), CTX)
    assert calm.mass.f == 0.0
    assert hot.mass.f > 0.3 and hot.features["count_1m"] == 7.0


def test_timing_unseen_hour_is_fraud_leaning():
    hist = [_ev(i, ts=T0 + timedelta(days=i)) for i in range(7)]  # always 09:00 UTC
    dna = _store(hist).view("A")
    s = TimingSensor()
    usual = s.evaluate(_ev(50, ts=T0 + timedelta(days=7)), dna, CTX).mass
    odd = s.evaluate(_ev(51, ts=T0 + timedelta(days=7, hours=-6)), dna, CTX).mass
    assert usual.l > 0.0 and usual.f == 0.0
    assert odd.f == pytest.approx(SensorParams().timing_a)


# ---- vacuous rules ------------------------------------------------------------------
@pytest.mark.parametrize("field", ["lat", "lon"])
@pytest.mark.parametrize("value", [None, ""])
def test_geo_lat_lon_missing_together(field, value):
    dna = _store([_ev(i) for i in range(6)]).view("A")
    out = GeoSensor().evaluate(_ev(9, **{field: value}), dna, CTX)
    assert out.mass == VACUOUS and out.vacuous_reason == "missing:lat,lon"


def test_missing_event_fields_named():
    dna = _store([_ev(i) for i in range(6)]).view("A")
    assert AmountSensor().evaluate(_ev(9, amount=None), dna, CTX).vacuous_reason == "missing:amount"
    assert CounterpartySensor().evaluate(_ev(9, dst=""), dna, CTX).vacuous_reason == "missing:dst"
    assert VelocitySensor().evaluate(_ev(9, ts=None), dna, CTX).vacuous_reason == "missing:ts"


def test_missing_dna_features_vacuous():
    dna = _store([_ev(i, lat=None, lon=None) for i in range(6)]).view("A")
    assert GeoSensor().evaluate(_ev(9), dna, CTX).vacuous_reason == "missing:dna.last_location"
    no_stats = replace(dna, amount=replace(dna.amount, w=0.0), iat=replace(dna.iat, w=0.0),
                       hour_hist=(0.0,) * 24)
    assert AmountSensor().evaluate(_ev(9), no_stats, CTX).vacuous_reason == "missing:dna.amount"
    assert VelocitySensor().evaluate(_ev(9), no_stats, CTX).vacuous_reason == "missing:dna.iat"
    assert TimingSensor().evaluate(_ev(9), no_stats, CTX).vacuous_reason == "missing:dna.hour_hist"


def test_short_history_vacuous_and_geo_halved():
    cfg = SensorConfig(min_history_count=5)
    dna = _store([_ev(i) for i in range(3)]).view("A")
    far = _ev(9, ts=T0 + timedelta(hours=3), lat=6.52, lon=3.38)
    for cls in (AmountSensor, VelocitySensor, TimingSensor, CounterpartySensor, DriftSensor):
        out = cls(cfg).evaluate(far, dna, CTX)
        assert out.mass == VACUOUS and out.vacuous_reason == "history:3", cls.name
    g = GeoSensor(cfg).evaluate(far, dna, CTX)
    full = GeoSensor(SensorConfig(min_history_count=1)).evaluate(far, dna, CTX)
    assert g.vacuous_reason == "history:3"
    assert g.mass.f == pytest.approx(full.mass.f / 2) and g.mass.t >= 0.5


def test_drift_reads_cpd_state():
    hist = [_ev(i) for i in range(6)]
    dna = _store(hist).view("A")
    det = ChangePointDetector()
    for e in hist:
        det.observe(e)
    st = det.state("A")
    s = DriftSensor()
    assert s.evaluate(_ev(9), dna, CTX).vacuous_reason == "missing:cpd_state"
    assert st.kind is CPDKind.NONE
    assert s.evaluate(_ev(9), dna, SensorContext(st)).mass == VACUOUS


def test_params_validation():
    with pytest.raises(ValueError):
        SensorParams(amount_a=0.8, amount_b=0.3)
    with pytest.raises(ValueError):
        SensorParams(cp_c1=0.4, cp_c2=0.35)
    with pytest.raises(ValueError):
        SensorParams(timing_p_ref=0.0)

