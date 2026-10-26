"""Sensor runner (design §4 "Runner", Req 5.1, 5.2, 5.12-5.14).

Every Sensor_Agent is invoked exactly once per event, in the order given. Each call is
wrapped in ``try``, timed with a monotonic clock (``time.perf_counter`` by default) and
its mass checked with ``is_valid``. Anything other than SUCCESS is replaced by the
Vacuous_Mass_Function and the failure type recorded; other sensors are untouched.

Python cannot preempt a synchronous call, so the time budget is detect-and-substitute:
a slow sensor still delays the event, but its output is discarded and flagged TIMEOUT.

Results are returned sorted by sensor name, so the output is independent of the order
the sensors were invoked in (Req 5.2, Property 15).
"""
from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from helix.fusion import VACUOUS, Contribution, ContributorType, is_valid
from helix.memory import DNAView

from .base import SensorAgent, SensorContext, SensorOutput

DEFAULT_BUDGET_MS = 10.0
MAX_ERROR_TEXT = 500

Clock = Callable[[], float]  # seconds, monotonic


class SensorOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    TIMEOUT = "TIMEOUT"

    def __str__(self) -> str:  # StrEnum-like behaviour on Python 3.10
        return self.value


@dataclass(frozen=True, slots=True)
class SensorResult:
    """One per Sensor_Agent invocation (Req 5.12). ``output.mass`` is vacuous unless
    ``outcome`` is SUCCESS; ``error_text`` describes the failure otherwise."""

    tx_id: str
    name: str
    outcome: SensorOutcome
    output: SensorOutput
    elapsed_ms: float
    error_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is SensorOutcome.SUCCESS


@dataclass(slots=True)
class SensorCounters:
    """Per-outcome counters, overall and per (sensor name, outcome)."""

    by_outcome: Counter = field(default_factory=Counter)
    by_sensor: Counter = field(default_factory=Counter)

    def record(self, r: SensorResult) -> None:
        self.by_outcome[r.outcome] += 1
        self.by_sensor[(r.name, r.outcome)] += 1


def _check_budget(budget_ms: float) -> float:
    b = float(budget_ms)
    if not (math.isfinite(b) and b > 0.0):
        raise ValueError(f"sensor runner: budget_ms={budget_ms!r} must be finite and > 0")
    return b


def _err(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}"
    return text if len(text) <= MAX_ERROR_TEXT else text[: MAX_ERROR_TEXT - 3] + "..."


def _features(out: object) -> dict:
    """Input features of a discarded output, kept for the record when readable."""
    if isinstance(out, SensorOutput) and isinstance(out.features, Mapping):
        try:
            return dict(out.features)
        except Exception:  # noqa: BLE001 - a hostile Mapping must not break the runner
            return {}
    return {}


def _run_one(
    sensor: SensorAgent, name: str, tx_id: str, ev: Any, dna: DNAView, ctx: SensorContext,
    budget_ms: float, clock: Clock,
) -> SensorResult:
    t0 = clock()
    try:
        out = sensor.evaluate(ev, dna, ctx)
    except Exception as e:  # noqa: BLE001 - any sensor failure is isolated (Req 5.14)
        elapsed = (clock() - t0) * 1000.0
        return SensorResult(tx_id, name, SensorOutcome.ERROR,
                            SensorOutput(VACUOUS, {}, "error"), elapsed, _err(e))
    elapsed = (clock() - t0) * 1000.0

    if elapsed > budget_ms:
        return SensorResult(
            tx_id, name, SensorOutcome.TIMEOUT,
            SensorOutput(VACUOUS, _features(out), "timeout"), elapsed,
            f"elapsed {elapsed:.3f} ms > budget {budget_ms:g} ms",
        )
    if not (isinstance(out, SensorOutput) and is_valid(out.mass)):
        got = out.mass if isinstance(out, SensorOutput) else type(out).__name__
        return SensorResult(
            tx_id, name, SensorOutcome.INVALID_OUTPUT,
            SensorOutput(VACUOUS, _features(out), "invalid_output"), elapsed,
            f"invalid output: {got!r}"[:MAX_ERROR_TEXT],
        )
    return SensorResult(tx_id, name, SensorOutcome.SUCCESS, out, elapsed)


def run_sensors(
    ev: Any,
    dna: DNAView,
    ctx: SensorContext,
    sensors: Sequence[SensorAgent],
    budget_ms: float = DEFAULT_BUDGET_MS,
    *,
    counters: SensorCounters | None = None,
    clock: Clock = time.perf_counter,
) -> list[SensorResult]:
    """Invoke each sensor exactly once (in the given order) and return one
    ``SensorResult`` per sensor, sorted by sensor name.

    Raises ``ValueError`` for a non-positive budget or duplicate sensor names (a
    duplicate would make the name-sorted output depend on invocation order).
    """
    budget = _check_budget(budget_ms)
    names = [str(getattr(s, "name")) for s in sensors]
    if len(set(names)) != len(names):
        dup = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(f"sensor runner: duplicate sensor names {dup}")
    tx_id = str(getattr(ev, "tx_id", ""))

    results = [_run_one(s, n, tx_id, ev, dna, ctx, budget, clock) for s, n in zip(sensors, names)]
    results.sort(key=lambda r: r.name)
    if counters is not None:
        for r in results:
            counters.record(r)
    return results


class SensorRunner:
    """Stateful convenience wrapper: fixed sensors, budget and clock plus counters."""

    def __init__(
        self,
        sensors: Sequence[SensorAgent],
        budget_ms: float = DEFAULT_BUDGET_MS,
        clock: Clock = time.perf_counter,
    ) -> None:
        self.sensors = list(sensors)
        self.budget_ms = _check_budget(budget_ms)
        self.clock = clock
        self.counters = SensorCounters()

    def run(self, ev: Any, dna: DNAView, ctx: SensorContext) -> list[SensorResult]:
        return run_sensors(ev, dna, ctx, self.sensors, self.budget_ms,
                           counters=self.counters, clock=self.clock)


def to_contributions(
    results: Sequence[SensorResult],
    weights: Mapping[str, float] | None = None,
    default: float = 1.0,
) -> list[Contribution]:
    """SENSOR contributions for the Evidence_Fuser, one per result (failed sensors
    contribute their substituted vacuous mass, which is neutral in fusion)."""
    w = weights or {}
    return [
        Contribution(r.name, ContributorType.SENSOR, r.output.mass, float(w.get(r.name, default)))
        for r in results
    ]


__all__ = [
    "DEFAULT_BUDGET_MS",
    "SensorCounters",
    "SensorOutcome",
    "SensorResult",
    "SensorRunner",
    "run_sensors",
    "to_contributions",
]
