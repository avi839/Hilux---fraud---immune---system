"""Pipeline integration of the Immune_System, without touching ``Helix.process``.

``attach_immune(helix, immune)`` replaces ``helix.stage1`` with an :class:`ImmuneStage1Engine`
that shares the original engine's Profile_Store, CPD, sensors, sensor clock and the *same*
Sensor_Weight dict, so the pipeline, the Feedback_Learner and replay keep seeing one state.
The engine differs from ``DefaultStage1Engine`` in two places:

* ``assess``: after the sensors run, the event's feature vector is evaluated against the
  active Antibody_Pool and every match joins stage-1 fusion as an ANTIBODY contribution
  (Req 21.8). For each match an extra SUCCESS ``SensorResult`` named ``antibody:<id>`` is
  appended to the returned results, so the Evidence_Chain entry carries ``antibody_id``,
  the matched dimensions and the tags (the Explanation_Engine's antibody template). Real
  sensor results stay first and in name order.
* ``commit``: after the DNA/CPD step the event's features are cached and offered to the
  Self_Set (REMEMBER); ``assess`` stays free of immune side effects other than match counts.

A feedback hook is registered on ``helix.feedback_hooks`` (after the Feedback_Learner's) so
labels credit/debit antibodies, purge CONFIRMED_FRAUD events from the Self_Set and trigger
clonal selection for low-risk CONFIRMED_FRAUD Cases. The antibody weights are the learner's
``other_weights`` dict when a learner is attached.

Replay exactness (honest note): stage-1 results that include antibody contributions can be
reproduced only if the Immune_System state that was live at that time is restored.
``ReplayService`` keeps using ``DefaultStage1Engine.from_snapshot`` (no antibodies) unless it
is given ``immune_engine_factory(snapshot)``, where ``snapshot`` is an
``ImmuneSystem.to_dict()`` taken at the replay start. HELIX does not persist pool snapshots
over time yet, so a timeline replay of a period in which antibodies fired shows the
sensor-only stage-1 assessments.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from helix.config import HelixConfig
from helix.fusion import FusedAssessment, FusionError, fuse
from helix.replay.engine import DefaultStage1Engine, EngineFactory
from helix.sensors import SensorContext, SensorResult, run_sensors, to_contributions
from helix.sensors.base import SensorOutput
from helix.sensors.runner import SensorOutcome
from helix.storage import Snapshot

from .features import feature_vector
from .system import AntibodyMatch, ImmuneSystem

log = logging.getLogger(__name__)


def match_result(tx_id: str, m: AntibodyMatch, tags: tuple[str, ...] = ()) -> SensorResult:
    """Evidence_Chain carrier for one antibody match (features for the explanation)."""
    feats = {"antibody_id": m.ab_id, "matched": ",".join(m.dims), "tags": ",".join(tags)}
    return SensorResult(tx_id, m.contributor_id, SensorOutcome.SUCCESS, SensorOutput(m.mass, feats, None), 0.0)


class ImmuneStage1Engine(DefaultStage1Engine):
    """``DefaultStage1Engine`` plus Antibody contributions in stage-1 fusion."""

    immune: ImmuneSystem

    def __init__(self, cfg: HelixConfig | None = None, *, immune: ImmuneSystem, **kw: Any) -> None:
        super().__init__(cfg, **kw)
        self.immune = immune
        self._pending: Optional[tuple[str, Any]] = None

    @classmethod
    def wrap(cls, base: DefaultStage1Engine, immune: ImmuneSystem) -> ImmuneStage1Engine:
        """Same state objects as ``base`` (profiles, cpd, sensors, weights dict, clock)."""
        eng = cls.__new__(cls)
        eng.__dict__.update(base.__dict__)
        eng.immune = immune
        eng._pending = None
        return eng

    def assess(self, ev: Any) -> tuple[FusedAssessment, list[SensorResult], list[FusionError]]:
        dna = self.profiles.view(ev.src)
        ctx = SensorContext(cpd_state=self.cpd.state(ev.src))
        results = run_sensors(ev, dna, ctx, self.sensors, self.cfg.fusion.sensor_budget_ms, clock=self.clock)
        fv = feature_vector(ev, results)
        matches = self.immune.evaluate(fv)
        contribs = to_contributions(results, self.weights) + [m.contribution for m in matches]
        fa, errors = fuse(contribs, self.cfg.fusion.high_conflict, tx_id=ev.tx_id, stage=1)
        self._pending = (ev.tx_id, fv)
        if matches:
            tags = {a.ab_id: a.tags for a in self.immune.pool()}
            results = results + [match_result(ev.tx_id, m, tags.get(m.ab_id, ())) for m in matches]
        return fa, results, errors

    def commit(self, ev: Any, *, withhold: bool = False) -> bool:
        applied = super().commit(ev, withhold=withhold)
        p, self._pending = self._pending, None
        if getattr(self, "suspend_self_set", False):  # Warm_Up_Mode: no Self_Set offer (Req 40.7)
            return applied
        fv = p[1] if p is not None and p[0] == ev.tx_id else feature_vector(ev, ())
        self.immune.observe(ev.tx_id, fv, ev)
        return applied


def attach_immune(helix: Any, immune: ImmuneSystem) -> ImmuneSystem:
    """Wire ``immune`` into a ``Helix`` pipeline. Idempotent for the same system."""
    if isinstance(helix.stage1, ImmuneStage1Engine):
        if helix.stage1.immune is immune:
            return immune
        helix.stage1.immune = immune
    else:
        helix.stage1 = ImmuneStage1Engine.wrap(helix.stage1, immune)
    learner = getattr(helix, "learner", None)
    other = getattr(learner, "other_weights", None)
    if other is not None:
        immune.weights = other  # Antibody Sensor_Weights live in the learner (Req 19, 21.8)
    if getattr(helix, "immune", None) is not immune:
        thr = float(helix.cfg.risk.alert_threshold)

        def on_label(case: Any, label: Any) -> None:
            try:
                immune.on_label(case, label, alert_threshold=thr)
            except Exception:  # noqa: BLE001 - learning must never break the label request
                log.exception("immune: label hook failed for %s", getattr(case, "case_id", "?"))

        helix.feedback_hooks.append(on_label)
    helix.immune = immune
    return immune


def immune_engine_factory(immune_snapshot: Mapping[str, Any], immune_cfg: Any = None, **kw: Any) -> EngineFactory:
    """``EngineFactory`` for ``ReplayService``/``recompute`` that restores the Immune_System
    from ``immune_snapshot`` (``ImmuneSystem.to_dict()`` at the replay start) for every engine,
    so replayed stage-1 assessments include the antibody contributions of that pool."""

    def factory(snapshot: Optional[Snapshot], weights: Mapping[str, float], cfg: HelixConfig) -> ImmuneStage1Engine:
        base = DefaultStage1Engine.from_snapshot(snapshot, weights, cfg, **kw)
        ic = immune_cfg if immune_cfg is not None else cfg.immune
        return ImmuneStage1Engine.wrap(base, ImmuneSystem.from_dict(immune_snapshot, ic))

    return factory


__all__ = ["ImmuneStage1Engine", "attach_immune", "immune_engine_factory", "match_result"]
