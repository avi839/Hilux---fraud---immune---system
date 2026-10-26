"""Fixtures and builders shared by the Immune_System tests."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Optional

import numpy as np

from helix.config.schema import ImmuneConfig
from helix.fusion import Contribution, ContributorType, Mass
from helix.immune import D, INDEX, ImmuneSystem
from helix.immune.features import MOTIF_DIMS
from helix.sim import ManualClock

T0 = datetime(2024, 3, 4, tzinfo=timezone.utc)


def vec(**dims: float) -> np.ndarray:
    """Feature vector with the given dimensions set, motif flags 0 and the rest missing (NaN)."""
    f = np.full(D, np.nan)
    for m in MOTIF_DIMS:
        f[INDEX[m]] = 0.0
    for k, v in dims.items():
        f[INDEX[k]] = v
    return f


def make_immune(seed: int = 0, clock: Optional[ManualClock] = None, **cfg: Any) -> ImmuneSystem:
    base: dict[str, Any] = dict(enabled=True, self_set_min=5, candidates_per_trigger=3)
    base.update(cfg)
    return ImmuneSystem(ImmuneConfig(**base), clock if clock is not None else ManualClock(T0), seed=seed)


def fake_case(ab_ids: Iterable[str], tx_ids: Iterable[str] = (), risk: float = 0.9, case_id: str = "C-1") -> Any:
    """A Case-shaped object whose single assessment lists the given antibodies as contributors."""
    contribs = [Contribution(f"antibody:{i}", ContributorType.ANTIBODY, Mass(0.5, 0.0, 0.5), 1.0) for i in ab_ids]
    return SimpleNamespace(case_id=case_id, tx_ids=set(tx_ids), risk_at_creation=risk,
                           assessments={"tx": SimpleNamespace(contributions=contribs)}, motifs={})


def seed_self(imm: ImmuneSystem, n: int, value: float = 0.1, prefix: str = "s") -> None:
    """Observe ``n`` self events at ``log_amount = value`` (hour dims present)."""
    for i in range(n):
        imm.observe(f"{prefix}{i}", vec(log_amount=value, hour_sin=0.5, hour_cos=0.5))


def activate_one(imm: ImmuneSystem, tx: str, value: float) -> list:
    """Observe an attack vector at ``log_amount = value``, record it as a miss, run selection."""
    imm.observe(tx, vec(log_amount=value, hour_sin=0.5, hour_cos=0.5))
    out = imm.on_miss(tx)
    imm.tick()
    return out
