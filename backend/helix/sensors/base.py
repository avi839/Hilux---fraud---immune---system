"""Sensor_Agent protocol, outputs, context, parameters and shared helpers (design §4).

Vacuous rules (checked before any formula, in this order):
1. a required event field is absent, ``None`` or ``""`` → vacuous, ``missing:<fields>``
   (latitude and longitude are reported, and treated as missing, together);
2. ``dna.count < Minimum_History_Count`` → vacuous, ``history:<count>`` (Geo instead
   halves its masses and keeps the ``history:<count>`` reason, so Θ ≥ 0.5);
3. a required Behavioural_DNA feature has no recorded value → vacuous,
   ``missing:dna.<feature>``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from helix.changepoint import CPDState
from helix.fusion import VACUOUS, Mass
from helix.memory import DNAView

Feature = float | str | None


@dataclass(frozen=True, slots=True)
class SensorOutput:
    mass: Mass
    features: Mapping[str, Feature] = field(default_factory=dict)
    #: ``"missing:lat,lon"`` | ``"missing:dna.iat"`` | ``"history:3"`` | None
    vacuous_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SensorContext:
    """Per-event context beyond the DNA snapshot. ``cpd_state`` is the account's
    Change_Point_Detector state (read by the Drift_Sensor only)."""

    cpd_state: CPDState | None = None


@runtime_checkable
class SensorAgent(Protocol):
    name: str

    def evaluate(self, ev: Any, dna_before: DNAView, ctx: SensorContext) -> SensorOutput: ...


def _check_ab(name: str, a: float, b: float) -> None:
    if not (0.0 < a < 1.0):
        raise ValueError(f"sensor params: {name}_a={a!r} must be in (0, 1)")
    if not (0.0 <= b < 1.0):
        raise ValueError(f"sensor params: {name}_b={b!r} must be in [0, 1)")
    if a + b > 1.0:
        raise ValueError(f"sensor params: {name}_a + {name}_b = {a + b!r} must be <= 1")


@dataclass(frozen=True, slots=True)
class SensorParams:
    """Formula parameters (design §4 table). Every pair satisfies ``a + b ≤ 1``.

    Defaults are tuned on ``docs/sample_transactions.jsonl``: Alice's T007 (€4,800 from
    Lagos 70 min after Berlin, unknown payee) gets clearly more FRAUD than LEGIT mass
    from Amount, Geo and Counterparty, while T001-T006 stay LEGIT-leaning.
    """

    # Amount: z = |x - μ| / max(σ, ε); mF = ramp(z - z0, a, s); mL = b·e^-z·(1 - mF/a)
    amount_a: float = 0.7
    amount_b: float = 0.3
    amount_z0: float = 2.0
    amount_s: float = 2.0
    amount_eps: float = 1.0  # currency units
    # Velocity: r = max_i c_i / max(E_min, W_i / mean_iat)
    velocity_a: float = 0.6
    velocity_b: float = 0.3
    velocity_r0: float = 2.0
    velocity_s: float = 2.0
    velocity_e_min: float = 1.0
    velocity_iat_eps_s: float = 1.0  # floor on mean inter-arrival (all-equal timestamps)
    # Timing: p = freq_h · freq_w against the uniform slot probability p_ref
    timing_a: float = 0.3
    timing_b: float = 0.2
    timing_p_ref: float = 1.0 / 168.0
    # Geo: mF = a(1 - vmax/v) above vmax; mL = b(1 - v/vmax) at or below
    geo_a: float = 0.8
    geo_b: float = 0.2
    # Counterparty: known → (c1, l1), unknown → (c2, 0), c2 > c1
    cp_c1: float = 0.0
    cp_l1: float = 0.3
    cp_c2: float = 0.35

    def __post_init__(self) -> None:
        for n in ("amount", "velocity", "timing", "geo"):
            _check_ab(n, getattr(self, f"{n}_a"), getattr(self, f"{n}_b"))
        for n in ("amount_s", "velocity_s", "amount_eps", "velocity_e_min",
                  "velocity_iat_eps_s", "timing_p_ref"):
            v = getattr(self, n)
            if not (math.isfinite(v) and v > 0.0):
                raise ValueError(f"sensor params: {n}={v!r} must be finite and > 0")
        for n in ("amount_z0", "velocity_r0"):
            v = getattr(self, n)
            if not (math.isfinite(v) and v >= 0.0):
                raise ValueError(f"sensor params: {n}={v!r} must be finite and >= 0")
        c1, l1, c2 = self.cp_c1, self.cp_l1, self.cp_c2
        if not (0.0 <= c1 < c2 < 1.0 and 0.0 <= l1 and c1 + l1 <= 1.0):
            raise ValueError("sensor params: need 0 <= cp_c1 < cp_c2 < 1 and cp_c1 + cp_l1 <= 1")


DEFAULT_PARAMS = SensorParams()


# ---- helpers ---------------------------------------------------------------------------
def mass_of(f: float, l: float) -> Mass:  # noqa: E741
    """Mass with Θ = 1 - f - l (clamped at 0 against rounding)."""
    return Mass(f, l, max(0.0, 1.0 - f - l))


def ramp(u: float, a: float, s: float) -> float:
    """``a·(1 - e^(-max(0, u)/s))``: 0 for u ≤ 0, non-decreasing, → a as u → ∞."""
    if not u > 0.0:  # also catches NaN
        return 0.0
    return a * -math.expm1(-u / s)


def is_missing(v: object) -> bool:
    return v is None or v == ""


def missing_fields(ev: Any, fields: Sequence[str]) -> list[str]:
    """Required event fields that are absent, None or "" (lat/lon reported together)."""
    miss = {f for f in fields if is_missing(getattr(ev, f, None))}
    if miss & {"lat", "lon"}:
        miss |= {"lat", "lon"}
    order = list(fields) + [f for f in ("lat", "lon") if f not in fields]
    return [f for f in order if f in miss]


def vacuous(reason: str | None, features: Mapping[str, Feature] | None = None) -> SensorOutput:
    return SensorOutput(VACUOUS, dict(features or {}), reason)


def missing_output(fields: Sequence[str]) -> SensorOutput:
    return vacuous("missing:" + ",".join(fields))


def history_output(count: int) -> SensorOutput:
    return vacuous(f"history:{count}", {"history_count": float(count)})
