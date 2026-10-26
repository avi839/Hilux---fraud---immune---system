"""Property tests for helix.sensors.runner (design Property 15, runner part)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.changepoint import ChangePointDetector
from helix.codec import TransactionEvent
from helix.fusion import VACUOUS, Mass, is_valid
from helix.memory import ProfileStore
from helix.sensors import (
    SENSOR_NAMES,
    SensorContext,
    SensorCounters,
    SensorOutcome,
    SensorOutput,
    default_sensors,
    run_sensors,
)

pytestmark = pytest.mark.property

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
MODES = ("ok", "raise", "timeout", "invalid")
EXPECTED = {
    "raise": SensorOutcome.ERROR,
    "timeout": SensorOutcome.TIMEOUT,
    "invalid": SensorOutcome.INVALID_OUTPUT,
}


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Forced:
    """Wraps a real sensor and forces a failure mode; counts invocations."""

    def __init__(self, inner, mode, clock, delay_ms, bad_out):
        self.inner, self.name, self.mode = inner, inner.name, mode
        self.clock, self.delay_ms, self.bad_out = clock, delay_ms, bad_out
        self.calls = 0

    def evaluate(self, ev, dna, ctx):
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError(f"forced {self.name}")
        if self.mode == "invalid":
            return self.bad_out
        out = self.inner.evaluate(ev, dna, ctx)
        if self.mode == "timeout":
            self.clock.t += self.delay_ms / 1000.0
        return out


unit = st.floats(0, 1, allow_nan=False)
invalid_masses = st.one_of(
    st.tuples(unit, unit, unit).filter(lambda m: abs(sum(m) - 1.0) > 1e-6).map(lambda m: Mass(*m)),
    st.tuples(st.floats(-1, -1e-6), unit).map(lambda p: Mass(p[0], p[1], 1.0 - p[0] - p[1])),
    st.just(Mass(float("nan"), 0.0, 1.0)),
)
bad_outputs = st.one_of(
    invalid_masses.map(SensorOutput),
    st.sampled_from([None, 0.5, "x", (0.0, 0.0, 1.0)]),
)


@st.composite
def scenarios(draw):
    """(current event, pre-update DNAView, SensorContext) for account A."""
    n = draw(st.integers(0, 10))
    store, det, t = ProfileStore(), ChangePointDetector(), T0
    for i in range(n + 1):
        t = t + timedelta(seconds=draw(st.integers(0, 86_400)))
        has_loc = draw(st.booleans())
        ev = TransactionEvent.model_construct(
            tx_id=f"T{i}", ts=t, src="A", dst=draw(st.sampled_from(["M0", "M1", "NEW"])),
            amount=Decimal(draw(st.integers(1, 10_000_000))) / 100, currency="EUR",
            channel="card",
            lat=draw(st.floats(-90, 90)) if has_loc else None,
            lon=draw(st.floats(-180, 180)) if has_loc else None,
        )
        if i == n:
            return ev, store.view("A"), SensorContext(det.state("A"))
        store.apply(ev)
        det.observe(ev)


def _key(out: SensorOutput):
    # repr() makes NaN features compare equal to themselves.
    return out.mass, out.vacuous_reason, repr(sorted(out.features.items()))


@given(
    sc=scenarios(),
    modes=st.lists(st.sampled_from(MODES), min_size=6, max_size=6),
    order=st.permutations(range(6)),
    budget_ms=st.floats(1, 1_000),
    excess_ms=st.floats(1e-3, 10_000),
    bad=bad_outputs,
)
def test_p15_sensor_independence(sc, modes, order, budget_ms, excess_ms, bad):
    # Feature: helix-fraud-immune-system, Property 15: Sensor independence
    """**Validates: Requirements 5.2, 5.14**"""
    ev, dna, ctx = sc
    baseline = {
        r.name: r for r in run_sensors(ev, dna, ctx, default_sensors(), budget_ms, clock=FakeClock())
    }
    assert all(r.outcome is SensorOutcome.SUCCESS for r in baseline.values())

    clk = FakeClock()
    real = default_sensors()
    forced = [Forced(real[i], modes[i], clk, budget_ms + excess_ms, bad) for i in range(6)]
    invoked = [forced[i] for i in order]
    counters = SensorCounters()
    res = run_sensors(ev, dna, ctx, invoked, budget_ms, counters=counters, clock=clk)

    # one result per sensor, each invoked exactly once, stable name order (Req 5.1)
    assert [r.name for r in res] == sorted(SENSOR_NAMES)
    assert all(f.calls == 1 for f in forced)
    mode_of = {f.name: f.mode for f in forced}
    for r in res:
        assert is_valid(r.output.mass)
        m = mode_of[r.name]
        if m == "ok":
            assert r.outcome is SensorOutcome.SUCCESS
            assert _key(r.output) == _key(baseline[r.name].output)
        else:
            assert r.outcome is EXPECTED[m]
            assert r.output.mass == VACUOUS and r.error_text
    assert sum(counters.by_outcome.values()) == 6
    for m, o in EXPECTED.items():
        assert counters.by_outcome[o] == modes.count(m)
