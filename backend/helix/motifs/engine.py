"""MotifEngine: runs every Motif_Detector for one new edge under a shared budget
and turns emitted instances into per-event Motif contributions (Req 11.6, 11.10).
"""

from __future__ import annotations

import math
from datetime import timedelta
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from types import MappingProxyType

from helix.fusion import Contribution, ContributorType, Mass
from helix.graph import Edge, GraphEvent, TemporalGraph
from helix.motifs.detectors import (
    FAN_MIN_COUNTERPARTIES,
    FAN_SPAN,
    ROUND_TRIP_MAX_ACCOUNTS,
    ROUND_TRIP_SPAN,
    FanInDetector,
    FanOutDetector,
    MotifDetector,
    MuleChainDetector,
    RoundTripDetector,
    StructuringDetector,
)
from helix.motifs.models import MotifInstance, MotifType

DEFAULT_BUDGET_MS = 20.0

# Configurable per-type mass on {FRAUD}; LEGIT is always 0 and the rest is Θ.
DEFAULT_MOTIF_MASSES: Mapping[MotifType, float] = MappingProxyType(
    {
        MotifType.ROUND_TRIP: 0.6,
        MotifType.FAN_OUT: 0.4,
        MotifType.FAN_IN: 0.4,
        MotifType.STRUCTURING: 0.6,
        MotifType.MULE_CHAIN: 0.7,
    }
)


def motif_mass(m: float) -> Mass:
    """``Mass(m, 0, 1 - m)``; ``m`` must be finite in (0, 1]."""
    if isinstance(m, bool) or not isinstance(m, (int, float)) or not math.isfinite(m) or not 0.0 < m <= 1.0:
        raise ValueError(f"motif FRAUD mass must be in (0, 1], got {m!r}")
    m = float(m)
    return Mass(m, 0.0, 1.0 - m)


def contributor_id(t: MotifType) -> str:
    return f"motif:{t.value}"


_NO_CONTRIBS: Mapping[str, tuple[Contribution, ...]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class EngineResult:
    instances: tuple[MotifInstance, ...]
    # tx_id -> one Contribution per Motif type the event participates in for this edge
    contributions: Mapping[str, tuple[Contribution, ...]] = field(default=_NO_CONTRIBS)
    truncated: frozenset[MotifType] = frozenset()  # types whose search hit the budget

    @property
    def is_truncated(self) -> bool:
        return bool(self.truncated)


EMPTY_RESULT = EngineResult(())


class MotifEngine:
    def __init__(
        self,
        budget_ms: float = DEFAULT_BUDGET_MS,
        reporting_threshold: float | str = 10_000,
        masses: Mapping[MotifType, float] | None = None,
        weights: Mapping[MotifType, float] | None = None,
        detectors: Sequence[MotifDetector] | None = None,
    ) -> None:
        if isinstance(budget_ms, bool) or not math.isfinite(budget_ms) or budget_ms <= 0:
            raise ValueError(f"motif_budget_ms must be positive, got {budget_ms!r}")
        self.budget_s = float(budget_ms) / 1000.0
        cfg = {**DEFAULT_MOTIF_MASSES, **(masses or {})}
        mass = {t: motif_mass(cfg[t]) for t in MotifType}
        self.weights = {t: 1.0 for t in MotifType} | dict(weights or {})
        for t, w in self.weights.items():
            if not (math.isfinite(w) and 0.0 <= w <= 1.0):
                raise ValueError(f"motif weight for {t} must be in [0, 1], got {w!r}")
        # Cheap, bounded detectors first so the DFS-heavy ones absorb any budget overrun.
        self.detectors: tuple[MotifDetector, ...] = tuple(
            detectors
            if detectors is not None
            else (
                StructuringDetector(mass[MotifType.STRUCTURING], reporting_threshold),
                FanOutDetector(mass[MotifType.FAN_OUT]),
                FanInDetector(mass[MotifType.FAN_IN]),
                MuleChainDetector(mass[MotifType.MULE_CHAIN]),
                RoundTripDetector(mass[MotifType.ROUND_TRIP]),
            )
        )

    @classmethod
    def from_config(cls, graph_cfg, **kw) -> "MotifEngine":
        """Build from a ``GraphConfig``: ``motif_budget_ms``, ``reporting_threshold`` and the
        fan / round-trip shape parameters (``fan_min_counterparties``, ``fan_span_s``,
        ``round_trip_max_accounts``, ``round_trip_span_s``). Fields missing on ``graph_cfg``
        keep the detector defaults."""
        if "detectors" not in kw:
            cfg = {**DEFAULT_MOTIF_MASSES, **(kw.get("masses") or {})}
            mass = {t: motif_mass(cfg[t]) for t in MotifType}
            k = int(getattr(graph_cfg, "fan_min_counterparties", FAN_MIN_COUNTERPARTIES))
            fan_span = timedelta(seconds=float(getattr(graph_cfg, "fan_span_s", FAN_SPAN.total_seconds())))
            rt_k = int(getattr(graph_cfg, "round_trip_max_accounts", ROUND_TRIP_MAX_ACCOUNTS))
            rt_span = timedelta(
                seconds=float(getattr(graph_cfg, "round_trip_span_s", ROUND_TRIP_SPAN.total_seconds()))
            )
            kw["detectors"] = (
                StructuringDetector(mass[MotifType.STRUCTURING], graph_cfg.reporting_threshold),
                FanOutDetector(mass[MotifType.FAN_OUT], k, fan_span),
                FanInDetector(mass[MotifType.FAN_IN], k, fan_span),
                MuleChainDetector(mass[MotifType.MULE_CHAIN]),
                RoundTripDetector(mass[MotifType.ROUND_TRIP], rt_k, rt_span),
            )
        return cls(budget_ms=graph_cfg.motif_budget_ms, reporting_threshold=graph_cfg.reporting_threshold, **kw)

    def on_event(self, ev: GraphEvent, g: TemporalGraph, deadline: float | None = None) -> EngineResult:
        return self.on_edge(Edge(ev.tx_id, ev.src, ev.dst, ev.ts, ev.amount, ev.currency), g, deadline)

    def on_edge(self, e: Edge, g: TemporalGraph, deadline: float | None = None) -> EngineResult:
        """Run all detectors for an edge already added to ``g``.

        An edge that is not in the graph (rejected as duplicate/too old, or a replay
        with different endpoints) yields no instances. The stored edge is used, not
        the argument, so a replay with altered attributes cannot forge a motif.
        """
        stored = g.g.adj.get(e.src, {}).get(e.dst, {}).get(e.tx_id)
        if stored is None:
            return EMPTY_RESULT
        e = Edge(e.tx_id, e.src, e.dst, stored["ts"], stored["amount"], stored["currency"])
        if deadline is None:
            deadline = perf_counter() + self.budget_s

        instances: list[MotifInstance] = []
        truncated: set[MotifType] = set()
        for det in self.detectors:
            res = det.on_edge(e, g, deadline)
            instances.extend(res.instances)
            if res.truncated:
                truncated.add(det.motif_type)
        return EngineResult(tuple(instances), self.contributions(instances), frozenset(truncated))

    def contributions(self, instances: Sequence[MotifInstance]) -> Mapping[str, tuple[Contribution, ...]]:
        per_tx: dict[str, dict[MotifType, Contribution]] = {}
        for inst in instances:
            c = Contribution(contributor_id(inst.motif_type), ContributorType.MOTIF, inst.mass,
                             self.weights[inst.motif_type])
            for tx in sorted(inst.tx_ids):
                per_tx.setdefault(tx, {}).setdefault(inst.motif_type, c)
        return MappingProxyType({tx: tuple(d.values()) for tx, d in per_tx.items()})
