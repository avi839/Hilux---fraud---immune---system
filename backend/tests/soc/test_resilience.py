"""Resilience_Mode (helix.soc.resilience, Req 37.5-37.8)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.codec import TransactionEvent
from helix.core import Helix
from helix.fusion import VACUOUS, is_valid
from helix.immune import ImmuneSystem, attach_immune
from helix.ingest import StreamIngestor
from helix.sensors.runner import SensorOutcome
from helix.sim.clock import ManualClock
from helix.soc import soc_config
from helix.soc.resilience import (
    FaultySensor,
    ResilienceMode,
    ResilienceRates,
    ResilientIngestor,
    TeeLatency,
    rates_from_doc,
)

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
FAST = settings(max_examples=60, deadline=None, derandomize=True, database=None)
SENSOR_NAMES = ("amount", "auth", "counterparty", "drift", "geo", "timing", "velocity")


def const_clock() -> float:
    return 0.0


def ev(i: int, src: str = "A", dst: str = "B", amount: str = "25.00", minutes: float = 0.0) -> TransactionEvent:
    return TransactionEvent(tx_id=f"T{i}", ts=T0 + timedelta(minutes=minutes if minutes else i), src=src, dst=dst,
                            amount=Decimal(amount), currency="EUR", channel="web", lat=52.5, lon=13.4,
                            device_id="D1")


def stream(n: int) -> list[TransactionEvent]:
    accts = ["A", "B", "C", "D"]
    return [ev(i, accts[i % 4], accts[(i + 1) % 4], f"{20 + (i * 37) % 900}.00") for i in range(n)]


def pipeline(**kw) -> Helix:
    return Helix(soc_config(), ManualClock(T0), sensor_clock=const_clock, **kw)


def test_disabled_by_default_and_passthrough():
    mode = ResilienceMode()
    h = pipeline()
    mode.install_sensors(h)
    assert not mode.enabled and all(isinstance(s, FaultySensor) for s in h.stage1.sensors)
    out = h.process(ev(1))
    assert all(r.outcome is SensorOutcome.SUCCESS for r in out.sensor_results)
    assert mode.counters.sensor_calls == 0
    assert list(mode.perturb(stream(5))) == stream(5)
    assert mode.status()["enabled"] is False


def test_failed_sensor_is_vacuous_and_processing_continues():
    mode = ResilienceMode()
    h = pipeline()
    mode.install_sensors(h)
    mode.install_latency(h)
    mode.enable(ResilienceRates(sensor_failure=1.0))
    outs = h.process_all(stream(6))
    assert len(outs) == 6
    for o in outs:
        assert {r.name for r in o.sensor_results} == set(SENSOR_NAMES)
        assert all(r.outcome is SensorOutcome.ERROR and r.output.mass == VACUOUS for r in o.sensor_results)
        assert "injected sensor failure" in o.sensor_results[0].error_text
        assert is_valid(o.stage1.mass) and is_valid(o.stage2.mass)
    c = mode.counters
    assert c.sensor_failures == c.sensor_calls == 6 * len(SENSOR_NAMES)
    assert c.failures_by_sensor["amount"] == 6
    st_ = mode.status()
    assert st_["latency_ms"]["since_start"]["END_TO_END"]["count"] == 6  # samples while enabled
    assert isinstance(h.latency, TeeLatency) and h.latency.all_time("END_TO_END").count == 6
    mode.disable()
    h.process(ev(99, minutes=500))
    assert mode.status()["latency_ms"]["since_start"]["END_TO_END"]["count"] == 6
    assert h.latency.all_time("END_TO_END").count == 7


def test_sensor_wrappers_are_shared_with_the_immune_engine_and_removable():
    h = pipeline()
    attach_immune(h, ImmuneSystem(clock=h.clock))
    base_names = [s.name for s in h.stage1.sensors]
    mode = ResilienceMode(fail_decider=lambda name, tx: name == "geo")
    assert mode.install_sensors(h) == len(base_names)
    assert mode.install_sensors(h) == len(base_names)  # idempotent
    assert [s.name for s in h.stage1.sensors] == base_names
    mode.enable()
    res = {r.name: r for r in h.process(ev(1)).sensor_results}
    assert res["geo"].outcome is SensorOutcome.ERROR and res["amount"].outcome is SensorOutcome.SUCCESS
    mode.uninstall_sensors()
    assert not any(isinstance(s, FaultySensor) for s in h.stage1.sensors)


def test_ingestor_proxy_delays_and_bursts_without_loss():
    clock = ManualClock(T0)
    ing = StreamIngestor(clock=clock)
    mode = ResilienceMode()
    proxy = mode.wrap_ingestor(ing)
    assert isinstance(proxy, ResilientIngestor) and mode.wrap_ingestor(proxy) is proxy
    mode.enable(ResilienceRates(delay=0.3, burst=0.3, delay_events=3, burst_size=4, seed=7))
    evs = stream(40)
    results = [proxy.submit(e) for e in evs]
    assert all(r.accepted for r in results)
    mode.disable()  # releases everything still held
    assert len(proxy) == 40 and ing.counters.accepted == 40 and mode.held == 0
    c = mode.counters
    assert c.delayed_events > 0 and c.bursts > 0 and c.released_delayed == c.delayed_events
    assert c.deferred_acks > 0 and mode.peak_queue_length > 0
    # the same seed and rates reorder the stream (delayed events arrive behind later ones)
    mode2 = ResilienceMode()
    mode2.enable(ResilienceRates(delay=0.3, burst=0.3, delay_events=3, burst_size=4, seed=7))
    shaped = list(mode2.perturb(evs))
    assert shaped != evs and sorted(e.tx_id for e in shaped) == sorted(e.tx_id for e in evs)
    # attribute writes are forwarded (e.g. PATCH /config swapping the ingest config)
    proxy.config = ing.config
    assert proxy.backpressure is ing.backpressure


def test_rates_validation():
    assert rates_from_doc({"sensor_failure_rate": 0.5, "seed": 3}).sensor_failure == 0.5
    with pytest.raises(ValueError) as e:
        rates_from_doc({"delay_rate": 1.5, "burst_size": 1, "sensor_failure_rate": True})
    msg = str(e.value)
    assert "delay_rate" in msg and "burst_size" in msg and "sensor_failure_rate" in msg
    with pytest.raises(ValueError):
        ResilienceMode().enable(ResilienceRates(delay_events=0))


def test_seeded_injection_is_reproducible():
    def run() -> list[tuple[str, str]]:
        mode = ResilienceMode()
        h = pipeline()
        mode.install_sensors(h)
        mode.enable(ResilienceRates(sensor_failure=0.3, seed=11))
        out = h.process_all(stream(12))
        return [(r.tx_id, r.name) for o in out for r in o.sensor_results if not r.ok]

    a, b = run(), run()
    assert a == b and a


# --- properties ----------------------------------------------------------------------------------------


@FAST
@given(n=st.integers(0, 60), delay=st.floats(0, 1), burst=st.floats(0, 1), delay_events=st.integers(1, 8),
       burst_size=st.integers(2, 8), seed=st.integers(0, 1000), disable_at=st.none() | st.integers(0, 60))
def test_shaper_releases_a_permutation_of_the_input(n, delay, burst, delay_events, burst_size, seed, disable_at):
    """For all rates and streams, delays and bursts only reorder events: every submitted event is
    released exactly once and nothing is invented.

    **Validates: Requirements 37.5, 37.6**
    """
    mode = ResilienceMode()
    mode.enable(ResilienceRates(delay=delay, burst=burst, delay_events=delay_events, burst_size=burst_size,
                                seed=seed))
    sh = mode.shaper()
    out = []
    for i in range(n):
        if disable_at is not None and i == disable_at:
            mode.enabled = False
        out += sh.push(i)
    out += sh.drain()
    assert Counter(out) == Counter(range(n))
    assert mode.counters.delayed_events + mode.counters.bursts <= max(n, 0)


@st.composite
def failure_patterns(draw):
    n = draw(st.integers(1, 14))
    fails = draw(st.sets(st.tuples(st.integers(0, n - 1), st.sampled_from(SENSOR_NAMES)), max_size=n * 7))
    amounts = draw(st.lists(st.integers(1, 500_000), min_size=n, max_size=n))
    accts = draw(st.lists(st.sampled_from(["A", "B", "C"]), min_size=n, max_size=n))
    return n, fails, amounts, accts


@FAST
@given(pat=failure_patterns(), rate=st.sampled_from([None, 0.0, 0.5, 1.0]), seed=st.integers(0, 50))
def test_fused_masses_stay_valid_under_any_failure_pattern(pat, rate, seed):
    """For all injected failure patterns, processing continues, each failed Sensor_Agent contributes
    the vacuous mass, and every Fused_Assessment has masses in [0, 1] summing to 1 within 1e-9.

    **Validates: Requirements 37.6, 37.8**
    """
    n, fails, amounts, accts = pat
    mode = ResilienceMode(fail_decider=None if rate is not None else
                          (lambda name, tx: (int(tx[1:]), name) in fails))
    h = pipeline()
    mode.install_sensors(h)
    mode.enable(ResilienceRates(sensor_failure=rate or 0.0, seed=seed))
    evs = [ev(i, accts[i], "Z" if accts[i] != "Z" else "Y", f"{amounts[i] / 100:.2f}") for i in range(n)]
    outs = h.process_all(evs)
    assert len(outs) == n
    for i, o in enumerate(outs):
        for fa in (o.stage1, o.stage2):
            m = fa.mass
            assert all(0.0 <= x <= 1.0 for x in (m.f, m.l, m.t)) and abs(m.f + m.l + m.t - 1.0) <= 1e-9
            assert is_valid(m)
        for r in o.sensor_results:
            if rate is None and (i, r.name) in fails:
                assert r.outcome is SensorOutcome.ERROR and r.output.mass == VACUOUS
            if not r.ok:
                assert r.output.mass == VACUOUS
