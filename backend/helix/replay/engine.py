"""Replay engine interface and the default stage-1 engine.

``ReplayEngine`` is the small interface ``recompute`` drives: one ``process(ev)`` per
event, returning that event's stage-1 Fused_Assessment. An ``EngineFactory`` builds a
fresh engine from a stored snapshot (or None for empty state), the Sensor_Weights in
effect at the replay start and the HELIX_Config. Engines may also offer
``set_weights(weights)``; ``recompute`` calls it as WeightRecords are passed.

``DefaultStage1Engine`` is the stage-1 PERCEIVE/REMEMBER path:

1. sensors read the pre-event Behavioural_DNA and CPD state of ``ev.src``;
2. their masses are fused (stage 1) with the current Sensor_Weights;
3. the event is applied to the Profile_Store, then observed by the CPD (a confirmed
   regime shift rebases the account's DNA through ``profile_store_rebaser``).

It has no side effects outside its own state (no Storage, publishing, Cases or
learning). Antibody contributions are not part of it yet (Immune_System, task 14.3).

Determinism note: the sensor runner substitutes a vacuous mass when a sensor exceeds
its wall-clock budget. Pass the same ``clock`` live and in replay (e.g. a constant
clock) when bit-exact reproduction is required; a live TIMEOUT cannot be reproduced.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from helix.changepoint import ChangePointDetector, profile_store_rebaser
from helix.config import HelixConfig
from helix.fusion import FusedAssessment, FusionError, fuse
from helix.memory import ProfileStore
from helix.sensors import (
    SensorAgent,
    SensorContext,
    SensorResult,
    default_sensors,
    run_sensors,
    to_contributions,
)
from helix.storage import Snapshot, profile_store_from_dict, profile_store_to_dict

Clock = Callable[[], float]

#: Key added to the DNA snapshot dict: the last pipeline ``seq_no`` included in it.
LAST_SEQ_KEY = "last_seq_no"


@runtime_checkable
class ReplayEngine(Protocol):
    def process(self, ev: Any) -> FusedAssessment: ...


EngineFactory = Callable[[Optional[Snapshot], Mapping[str, float], HelixConfig], ReplayEngine]


class DefaultStage1Engine:
    def __init__(
        self,
        cfg: HelixConfig | None = None,
        *,
        profiles: ProfileStore | None = None,
        cpd: ChangePointDetector | None = None,
        sensors: Sequence[SensorAgent] | None = None,
        weights: Mapping[str, float] | None = None,
        clock: Clock = time.perf_counter,
    ) -> None:
        self.cfg = cfg or HelixConfig()
        self.profiles = profiles if profiles is not None else ProfileStore.from_config(self.cfg)
        self.cpd = cpd if cpd is not None else ChangePointDetector.from_config(self.cfg)
        self.cpd.on_regime_shift = profile_store_rebaser(self.profiles)
        self.sensors = list(sensors) if sensors is not None else default_sensors(self.cfg)
        self.weights: dict[str, float] = dict(weights or {})
        self.clock = clock

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Snapshot | None,
        weights: Mapping[str, float],
        cfg: HelixConfig,
        *,
        clock: Clock = time.perf_counter,
    ) -> DefaultStage1Engine:
        """``EngineFactory``: restore DNA + CPD from ``snapshot`` (empty state if None)."""
        if snapshot is None:
            return cls(cfg, weights=weights, clock=clock)
        profiles = profile_store_from_dict(snapshot.dna)
        cpd = ChangePointDetector.from_config(cfg)
        cpd.load_dict(snapshot.cpd)
        return cls(cfg, profiles=profiles, cpd=cpd, weights=weights, clock=clock)

    def set_weights(self, weights: Mapping[str, float]) -> None:
        self.weights = dict(weights)

    def process(self, ev: Any) -> FusedAssessment:
        fa, _results, _errors = self.assess(ev)
        self.commit(ev)
        return fa

    def assess(self, ev: Any) -> tuple[FusedAssessment, list[SensorResult], list[FusionError]]:
        """PERCEIVE: sensors on the pre-event DNA / CPD state, then stage-1 fusion.
        Reads state only; ``commit`` applies the event afterwards."""
        dna = self.profiles.view(ev.src)
        ctx = SensorContext(cpd_state=self.cpd.state(ev.src))
        results = run_sensors(
            ev, dna, ctx, self.sensors, self.cfg.fusion.sensor_budget_ms, clock=self.clock
        )
        fa, errors = fuse(
            to_contributions(results, self.weights),
            self.cfg.fusion.high_conflict,
            tx_id=ev.tx_id,
            stage=1,
        )
        return fa, results, errors

    def commit(self, ev: Any, *, withhold: bool = False) -> bool:
        """REMEMBER: apply ``ev`` to the Profile_Store (or withhold it up front), then step
        the CPD. Returns True if the event was applied to the DNA."""
        applied = self.profiles.apply(ev, withhold=withhold)
        self.cpd.observe(ev)
        return applied

    def snapshot_state(self, last_seq_no: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(dna, cpd)`` dicts for ``Storage.snapshot``. ``last_seq_no`` (the last
        processed pipeline seq_no) lets ``recompute`` resume exactly after it."""
        dna = profile_store_to_dict(self.profiles)
        if last_seq_no is not None:
            dna[LAST_SEQ_KEY] = int(last_seq_no)
        return dna, self.cpd.to_dict()


__all__ = ["LAST_SEQ_KEY", "DefaultStage1Engine", "EngineFactory", "ReplayEngine"]
