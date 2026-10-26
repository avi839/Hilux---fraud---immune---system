"""The six Sensor_Agents (design §4 formula table, Req 5.3-5.11, 5.15, 7.1-7.4).

Each sensor is a pure function of (event, pre-update DNAView, SensorContext): it never
mutates its inputs and keeps no state, so invocation order and other sensors' failures
cannot change its output (Req 5.2). The pure ``*_mass`` helpers carry the formulas and
are what the monotonicity properties exercise directly.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from helix.changepoint import drift_mass
from helix.config.schema import ChangePointConfig, HelixConfig, SensorConfig
from helix.memory import DNAView

from .base import (
    DEFAULT_PARAMS,
    SensorContext,
    SensorOutput,
    SensorParams,
    history_output,
    mass_of,
    missing_fields,
    missing_output,
    ramp,
    vacuous,
)

EARTH_RADIUS_KM = 6371.0088  # IUGG mean radius
VELOCITY_WINDOWS_S: tuple[float, float, float] = (60.0, 3_600.0, 86_400.0)
VELOCITY_LABELS = ("1m", "1h", "24h")


# ---- pure formulas -----------------------------------------------------------------
def amount_mass(z: float, p: SensorParams = DEFAULT_PARAMS) -> tuple[float, float]:
    mf = ramp(z - p.amount_z0, p.amount_a, p.amount_s)
    ml = p.amount_b * math.exp(-z) * (1.0 - mf / p.amount_a)
    return mf, ml


def velocity_ratio(counts: Sequence[float], expected: Sequence[float]) -> float:
    return max(c / e for c, e in zip(counts, expected))


def velocity_mass(
    counts: Sequence[float], expected: Sequence[float], p: SensorParams = DEFAULT_PARAMS
) -> tuple[float, float]:
    r = velocity_ratio(counts, expected)
    mf = ramp(r - p.velocity_r0, p.velocity_a, p.velocity_s)
    ml = p.velocity_b * math.exp(-r) * (1.0 - mf / p.velocity_a)
    return mf, ml


def timing_mass(freq_hour: float, freq_weekday: float, p: SensorParams = DEFAULT_PARAMS) -> tuple[float, float]:
    q = freq_hour * freq_weekday / p.timing_p_ref
    mf = p.timing_a * min(1.0, max(0.0, 1.0 - q))
    ml = p.timing_b * min(1.0, max(0.0, q - 1.0))
    return mf, ml


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def implied_speed_kmh(d_km: float, dt_h: float) -> float:
    """``0`` if d = 0, ``inf`` if dt = 0 and d > 0, else ``d / |dt|`` (Req 5.6, 5.8)."""
    if d_km <= 0.0:
        return 0.0
    dt = abs(dt_h)
    return math.inf if dt == 0.0 else d_km / dt


def geo_mass(v: float, vmax: float, p: SensorParams = DEFAULT_PARAMS) -> tuple[float, float]:
    if v <= vmax:
        return 0.0, p.geo_b * (1.0 - v / vmax)
    return p.geo_a * (1.0 - vmax / v), 0.0  # v = inf → a


# ---- sensors -----------------------------------------------------------------------
class _Base:
    name: str = ""

    def __init__(self, cfg: SensorConfig | None = None, params: SensorParams = DEFAULT_PARAMS) -> None:
        cfg = cfg or SensorConfig()
        self.min_history = int(cfg.min_history_count)
        self.params = params

    def __repr__(self) -> str:
        return f"{type(self).__name__}(min_history={self.min_history})"


class AmountSensor(_Base):
    name = "amount"

    def evaluate(self, ev: Any, dna_before: DNAView, ctx: SensorContext) -> SensorOutput:
        if miss := missing_fields(ev, ("amount",)):
            return missing_output(miss)
        if dna_before.count < self.min_history:
            return history_output(dna_before.count)
        st = dna_before.amount
        if st.w <= 0.0:
            return missing_output(["dna.amount"])
        x = float(ev.amount)
        sigma = math.sqrt(max(st.var, 0.0))
        z = abs(x - st.mean) / max(sigma, self.params.amount_eps)
        mf, ml = amount_mass(z, self.params)
        feats = {"amount": x, "mean": st.mean, "std": sigma, "z": z}
        return SensorOutput(mass_of(mf, ml), feats, None)


class VelocitySensor(_Base):
    name = "velocity"

    @staticmethod
    def window_counts(ts: datetime, recent: Sequence[datetime]) -> tuple[int, int, int]:
        """Counts in ``(ts - W, ts]`` over the pre-update recent timestamps, plus the
        current event. Late events beyond the DNA's 24 h recent window may undercount."""
        hi = bisect_right(recent, ts)
        out = []
        for w in VELOCITY_WINDOWS_S:
            lo = bisect_right(recent, ts - timedelta(seconds=w))
            out.append(hi - lo + 1)
        return out[0], out[1], out[2]

    def evaluate(self, ev: Any, dna_before: DNAView, ctx: SensorContext) -> SensorOutput:
        if miss := missing_fields(ev, ("ts",)):
            return missing_output(miss)
        if dna_before.count < self.min_history:
            return history_output(dna_before.count)
        if dna_before.iat.w <= 0.0:
            return missing_output(["dna.iat"])
        p = self.params
        mean_iat = max(dna_before.iat.mean, p.velocity_iat_eps_s)
        counts = self.window_counts(ev.ts, dna_before.recent_ts)
        expected = tuple(max(p.velocity_e_min, w / mean_iat) for w in VELOCITY_WINDOWS_S)
        mf, ml = velocity_mass(counts, expected, p)
        feats: dict[str, float | str | None] = {"mean_iat_s": mean_iat}
        for lbl, c, e in zip(VELOCITY_LABELS, counts, expected):
            feats[f"count_{lbl}"] = float(c)
            feats[f"expected_{lbl}"] = e
        feats["ratio"] = velocity_ratio(counts, expected)
        return SensorOutput(mass_of(mf, ml), feats, None)


class TimingSensor(_Base):
    name = "timing"

    def evaluate(self, ev: Any, dna_before: DNAView, ctx: SensorContext) -> SensorOutput:
        if miss := missing_fields(ev, ("ts",)):
            return missing_output(miss)
        if dna_before.count < self.min_history:
            return history_output(dna_before.count)
        th, tw = math.fsum(dna_before.hour_hist), math.fsum(dna_before.weekday_hist)
        if not th > 0.0:
            return missing_output(["dna.hour_hist"])
        if not tw > 0.0:
            return missing_output(["dna.weekday_hist"])
        utc = ev.ts.astimezone(timezone.utc)
        fh = dna_before.hour_hist[utc.hour] / th
        fw = dna_before.weekday_hist[utc.weekday()] / tw
        mf, ml = timing_mass(fh, fw, self.params)
        feats = {
            "hour": float(utc.hour),
            "weekday": float(utc.weekday()),
            "freq_hour": fh,
            "freq_weekday": fw,
            "p": fh * fw,
        }
        return SensorOutput(mass_of(mf, ml), feats, None)


class GeoSensor(_Base):
    name = "geo"

    def __init__(self, cfg: SensorConfig | None = None, params: SensorParams = DEFAULT_PARAMS) -> None:
        super().__init__(cfg, params)
        self.vmax = float((cfg or SensorConfig()).plausible_speed_kmh)

    def evaluate(self, ev: Any, dna_before: DNAView, ctx: SensorContext) -> SensorOutput:
        if miss := missing_fields(ev, ("lat", "lon", "ts")):
            return missing_output(miss)
        loc = dna_before.last_location
        if loc is None:
            return missing_output(["dna.last_location"])
        lat0, lon0, t0 = loc
        d = haversine_km(lat0, lon0, float(ev.lat), float(ev.lon))
        dt_h = abs((ev.ts - t0).total_seconds()) / 3600.0
        v = implied_speed_kmh(d, dt_h)
        mf, ml = geo_mass(v, self.vmax, self.params)
        reason = None
        if dna_before.count < self.min_history:  # Req 7.4: halve, Θ ≥ 0.5
            mf, ml, reason = mf / 2.0, ml / 2.0, f"history:{dna_before.count}"
        feats = {"distance_km": d, "dt_h": dt_h, "speed_kmh": v, "vmax_kmh": self.vmax}
        return SensorOutput(mass_of(mf, ml), feats, reason)


class CounterpartySensor(_Base):
    name = "counterparty"

    def evaluate(self, ev: Any, dna_before: DNAView, ctx: SensorContext) -> SensorOutput:
        if miss := missing_fields(ev, ("dst",)):
            return missing_output(miss)
        if dna_before.count < self.min_history:
            return history_output(dna_before.count)
        p = self.params
        known = ev.dst in dna_before.counterparties
        mf, ml = (p.cp_c1, p.cp_l1) if known else (p.cp_c2, 0.0)
        feats = {"dst": ev.dst, "known": 1.0 if known else 0.0,
                 "n_counterparties": float(len(dna_before.counterparties))}
        return SensorOutput(mass_of(mf, ml), feats, None)


class DriftSensor(_Base):
    name = "drift"

    def __init__(
        self,
        cfg: SensorConfig | None = None,
        params: SensorParams = DEFAULT_PARAMS,
        cp_cfg: ChangePointConfig | None = None,
    ) -> None:
        super().__init__(cfg, params)
        self.cp_cfg = cp_cfg or ChangePointConfig()

    def evaluate(self, ev: Any, dna_before: DNAView, ctx: SensorContext) -> SensorOutput:
        if dna_before.count < self.min_history:
            return history_output(dna_before.count)
        st = ctx.cpd_state
        if st is None:
            return missing_output(["cpd_state"])
        m = drift_mass(st, self.cp_cfg, dna_before.count, self.min_history)
        feats = {
            "cpd_kind": str(st.kind),
            "candidate_tx": None if st.candidate is None else st.candidate.tx_id,
        }
        # NONE / no candidate is vacuous but not a missing-data condition: reason None.
        return SensorOutput(m, feats, None)


SENSOR_CLASSES = (AmountSensor, VelocitySensor, TimingSensor, GeoSensor, CounterpartySensor, DriftSensor)
SENSOR_NAMES = tuple(c.name for c in SENSOR_CLASSES)


def default_sensors(
    cfg: HelixConfig | None = None, params: SensorParams = DEFAULT_PARAMS, *, auth_history: Any = None
) -> list[Any]:
    """The six Sensor_Agents in design order, built from a HelixConfig, plus the
    Auth_Sensor when ``cfg.soc.enabled`` (Req 28.8). ``auth_history`` is the pipeline's
    ``AuthHistory``; without one the Auth_Sensor reads an empty history (always vacuous)."""
    cfg = cfg or HelixConfig()
    s = cfg.sensors
    out: list[Any] = [
        AmountSensor(s, params),
        VelocitySensor(s, params),
        TimingSensor(s, params),
        GeoSensor(s, params),
        CounterpartySensor(s, params),
        DriftSensor(s, params, cfg.changepoint),
    ]
    if cfg.soc.enabled:
        from .auth import AuthHistory, AuthSensor  # local: keeps the default import path unchanged

        out.append(AuthSensor(auth_history if auth_history is not None else AuthHistory(cfg.soc), cfg.soc))
    return out


__all__ = [
    "EARTH_RADIUS_KM",
    "VELOCITY_WINDOWS_S",
    "AmountSensor",
    "CounterpartySensor",
    "DriftSensor",
    "GeoSensor",
    "SENSOR_CLASSES",
    "SENSOR_NAMES",
    "TimingSensor",
    "VelocitySensor",
    "amount_mass",
    "default_sensors",
    "geo_mass",
    "haversine_km",
    "implied_speed_kmh",
    "timing_mass",
    "vacuous",
    "velocity_mass",
    "velocity_ratio",
]
