"""Shared builders for the Req 41 tests (Auto_Triage, Label_Suggester, bulk feedback)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from helix.codec import TransactionEvent
from helix.config import HelixConfig
from helix.core import Helix
from helix.fusion import FusedAssessment, Mass
from helix.motifs import MotifEngine
from helix.sensors import SensorOutput
from helix.sim.clock import ManualClock

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


class RiskSensor:
    """Test sensor: the event's ``mcc`` holds the intended stage-1 risk in percent."""

    name = "fixed"

    def evaluate(self, ev, dna_before, ctx):  # noqa: ANN001 - SensorAgent protocol
        r = int(ev.mcc) / 100
        m = Mass(2 * r - 1, 0.0, 2 - 2 * r) if r >= 0.5 else Mass(0.0, 1 - 2 * r, 2 * r)
        return SensorOutput(m, {"r": r})


def tx(n: int, src: str, dst: str, r: int, ts: datetime, amount: str = "100.00") -> TransactionEvent:
    return TransactionEvent.model_validate({"tx_id": f"T{n}", "ts": ts.isoformat(), "src": src, "dst": dst,
                                            "amount": amount, "currency": "USD", "mcc": f"{r:04d}"})


def fixed_helix(cfg: Optional[HelixConfig] = None, clock: Optional[ManualClock] = None, **kw) -> Helix:
    kw.setdefault("motif_engine", MotifEngine(detectors=()))
    return Helix(cfg or HelixConfig(), clock or ManualClock(T0), sensors=[RiskSensor()],
                 sensor_clock=lambda: 0.0, **kw)


def fa(tx_id: str, r: float) -> FusedAssessment:
    """Stage-1 assessment whose pignistic risk is exactly ``r``."""
    m = Mass(2 * r - 1, 0.0, 2 - 2 * r) if r >= 0.5 else Mass(0.0, 1 - 2 * r, 2 * r)
    return FusedAssessment(tx_id, 1, m, m.f, m.f + m.t, m.t, 0.0, False, ())


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)
