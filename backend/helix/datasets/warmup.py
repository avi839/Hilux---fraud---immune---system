"""Warm_Up_Mode report (design §25, Req 40.7, 40.8).

While ``Helix.warmup`` is set the pipeline still runs SENSE through REASON (Behavioural_DNA,
Change_Point_Detector state, Temporal_Graph) but skips Case creation and the Self_Set offer.
Warm-up does not consult labels (ground truth stays out of the detection path), so
historical fraud enters Behavioural_DNA: a documented limitation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True, slots=True)
class WarmupReport:
    events: int
    duration_s: float
    events_per_s: float
    accounts: int
    accounts_ready: int  # applied DNA count >= Minimum_History_Count
    until: Optional[datetime]
    min_history: int = 5


def accounts_ready(profiles: Any, min_history: int) -> tuple[int, int]:
    """(accounts with a Behavioural_DNA, accounts with >= ``min_history`` applied events)."""
    accts = profiles.accounts()
    ready = sum(1 for a in accts if profiles.view(a).count >= min_history)
    return len(accts), ready


def build_report(helix: Any, events: int, duration_s: float, until: Optional[datetime]) -> WarmupReport:
    mh = int(helix.cfg.sensors.min_history_count)
    n, ready = accounts_ready(helix.profiles, mh)
    eps = events / duration_s if duration_s > 0 else 0.0
    return WarmupReport(events=events, duration_s=duration_s, events_per_s=eps, accounts=n,
                        accounts_ready=ready, until=until, min_history=mh)


__all__ = ["WarmupReport", "accounts_ready", "build_report"]
