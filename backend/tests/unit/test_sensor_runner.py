"""Unit tests for helix.sensors.runner (design §4 "Runner", Req 5.1, 5.2, 5.12-5.14)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from helix.codec import TransactionEvent
from helix.config.schema import FusionConfig
from helix.fusion import VACUOUS, ContributorType, Mass, fuse
from helix.memory import ProfileStore
from helix.sensors import (
    SENSOR_NAMES,
    SensorContext,
    SensorCounters,
    SensorOutcome,
    SensorOutput,
    SensorRunner,
    default_sensors,
    run_sensors,
    to_contributions,
)

T0 = datetime(2024, 6, 3, 9, 0, tzinfo=timezone.utc)
CTX = SensorContext()
O = SensorOutcome


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance_ms(self, ms: float) -> None:
        self.t += ms / 1000.0


class Stub:
    """Minimal Sensor_Agent: returns ``out`` (or raises it), optionally advancing a clock."""

    def __init__(self, name, out=None, clock=None, delay_ms=0.0):
        self.name, self.out, self.clock, self.delay_ms = name, out, clock, delay_ms
        self.calls = 0

    def evaluate(self, ev, dna, ctx):
        self.calls += 1
        if self.clock is not None:
            self.clock.advance_ms(self.delay_ms)
        if isinstance(self.out, BaseException):
            raise self.out
        return self.out


def _ev(i: int = 0, **kw) -> TransactionEvent:
    base = dict(
        tx_id=f"X{i}", ts=T0 + timedelta(hours=i), src="A", dst="M1",
        amount=Decimal("50.00"), currency="EUR", channel="card", lat=52.52, lon=13.40,
    )
    base.update(kw)
    return TransactionEvent.model_construct(**base)


@pytest.fixture
def scenario():
    store = ProfileStore()
    for i in range(8):
        store.apply(_ev(i))
    return _ev(20, amount=Decimal("4800.00"), dst="NEW", lat=6.52, lon=3.38), store.view("A")


GOOD = SensorOutput(Mass(0.2, 0.3, 0.5), {"x": 1.0})


def test_real_sensors_all_succeed_sorted_and_match_direct_evaluate(scenario):
    ev, dna = scenario
    sensors = default_sensors()
    res = run_sensors(ev, dna, CTX, sensors, 1_000)
    assert [r.name for r in res] == sorted(SENSOR_NAMES)
    by = {s.name: s for s in sensors}
    for r in res:
        assert r.outcome is O.SUCCESS and r.ok and r.error_text is None
        assert r.tx_id == "X20"
        assert r.output == by[r.name].evaluate(ev, dna, CTX)
        assert r.elapsed_ms >= 0.0


def test_raise_becomes_vacuous_error_and_others_unchanged(scenario):
    ev, dna = scenario
    sensors = default_sensors()
    base = {r.name: r for r in run_sensors(ev, dna, CTX, sensors, 1_000)}
    boom = Stub(sensors[0].name, ValueError("kaput"))
    res = {r.name: r for r in run_sensors(ev, dna, CTX, [boom, *sensors[1:]], 1_000)}
    r = res[boom.name]
    assert r.outcome is O.ERROR and r.output.mass == VACUOUS
    assert r.error_text == "ValueError: kaput"
    for n in base.keys() - {boom.name}:
        assert res[n].ok and res[n].output == base[n].output


def test_timeout_is_detected_and_substituted_with_fake_clock():
    clk = FakeClock()
    at = Stub("at_budget", GOOD, clk, delay_ms=10.0)
    over = Stub("over_budget", GOOD, clk, delay_ms=10.5)
    res = {r.name: r for r in run_sensors(_ev(), None, CTX, [over, at], 10.0, clock=clk)}
    assert res["at_budget"].outcome is O.SUCCESS and res["at_budget"].output == GOOD
    assert res["at_budget"].elapsed_ms == pytest.approx(10.0)
    t = res["over_budget"]
    assert t.outcome is O.TIMEOUT and t.output.mass == VACUOUS
    assert t.output.vacuous_reason == "timeout" and t.output.features == {"x": 1.0}
    assert t.elapsed_ms == pytest.approx(10.5) and "budget" in t.error_text


def test_error_takes_precedence_over_timeout():
    clk = FakeClock()
    s = Stub("slow_raise", RuntimeError("x"), clk, delay_ms=50.0)
    (r,) = run_sensors(_ev(), None, CTX, [s], 10.0, clock=clk)
    assert r.outcome is O.ERROR and r.elapsed_ms == pytest.approx(50.0)


@pytest.mark.parametrize("bad", [
    SensorOutput(Mass(0.6, 0.6, 0.0)),            # sums to 1.2
    SensorOutput(Mass(-0.1, 0.6, 0.5)),           # negative mass
    SensorOutput(Mass(float("nan"), 0.0, 1.0)),   # NaN
    SensorOutput(Mass(0.5, 0.5, 1e-6)),           # outside 1e-9 tolerance
    SensorOutput((0.0, 0.0, 1.0)),                # not a Mass
    None,                                         # not a SensorOutput
    Mass(0.0, 0.0, 1.0),                          # bare Mass, not a SensorOutput
])
def test_invalid_output_becomes_vacuous(bad):
    (r,) = run_sensors(_ev(), None, CTX, [Stub("bad", bad)], 10.0, clock=FakeClock())
    assert r.outcome is O.INVALID_OUTPUT and r.output.mass == VACUOUS
    assert r.output.vacuous_reason == "invalid_output" and r.error_text


def test_within_tolerance_is_valid():
    out = SensorOutput(Mass(0.5, 0.5, 5e-10))
    (r,) = run_sensors(_ev(), None, CTX, [Stub("ok", out)], 10.0, clock=FakeClock())
    assert r.outcome is O.SUCCESS and r.output is out


def test_each_sensor_invoked_exactly_once_in_given_order():
    order = []

    class Rec(Stub):
        def evaluate(self, ev, dna, ctx):
            order.append(self.name)
            return super().evaluate(ev, dna, ctx)

    ss = [Rec("c", GOOD), Rec("a", RuntimeError()), Rec("b", None)]
    res = run_sensors(_ev(), None, CTX, ss, 10.0, clock=FakeClock())
    assert order == ["c", "a", "b"] and all(s.calls == 1 for s in ss)
    assert [r.name for r in res] == ["a", "b", "c"]


def test_counters_per_outcome_accumulate():
    clk = FakeClock()
    runner = SensorRunner(
        [Stub("ok", GOOD), Stub("err", KeyError("k")), Stub("inv", None),
         Stub("slow", GOOD, clk, delay_ms=99.0)],
        budget_ms=10.0, clock=clk,
    )
    runner.run(_ev(0), None, CTX)
    runner.run(_ev(1), None, CTX)
    c = runner.counters
    assert c.by_outcome == {O.SUCCESS: 2, O.ERROR: 2, O.INVALID_OUTPUT: 2, O.TIMEOUT: 2}
    assert c.by_sensor[("slow", O.TIMEOUT)] == 2 and c.by_sensor[("ok", O.SUCCESS)] == 2

    extra = SensorCounters()
    run_sensors(_ev(), None, CTX, [Stub("ok", GOOD)], 10.0, counters=extra, clock=clk)
    assert extra.by_outcome == {O.SUCCESS: 1}


def test_rejects_duplicate_names_and_bad_budget():
    with pytest.raises(ValueError, match="duplicate"):
        run_sensors(_ev(), None, CTX, [Stub("a", GOOD), Stub("a", GOOD)], 10.0)
    for b in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="budget"):
            run_sensors(_ev(), None, CTX, [Stub("a", GOOD)], b)


def test_base_exception_is_not_swallowed():
    with pytest.raises(KeyboardInterrupt):
        run_sensors(_ev(), None, CTX, [Stub("a", KeyboardInterrupt())], 10.0)


def test_default_budget_comes_from_config_default():
    from helix.sensors import DEFAULT_BUDGET_MS
    assert DEFAULT_BUDGET_MS == FusionConfig().sensor_budget_ms


def test_to_contributions_weights_and_fusable(scenario):
    ev, dna = scenario
    res = run_sensors(ev, dna, CTX, [Stub("zz", RuntimeError()), *default_sensors()], 1_000)
    cs = to_contributions(res, {"amount": 0.5}, default=0.8)
    assert [c.contributor_id for c in cs] == [r.name for r in res]
    assert all(c.kind is ContributorType.SENSOR for c in cs)
    w = {c.contributor_id: c.weight for c in cs}
    assert w["amount"] == 0.5 and w["geo"] == 0.8 and w["zz"] == 0.8
    assert next(c for c in cs if c.contributor_id == "zz").mass == VACUOUS
    assert to_contributions(res)[0].weight == 1.0
    _, errors = fuse(cs, 0.95, tx_id=ev.tx_id, stage=1)
    assert errors == []
