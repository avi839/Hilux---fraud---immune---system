"""ReplayService facade (design §15): Case lookup + config around the replay functions."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Mapping, Optional

from helix.config import HelixConfig
from helix.fusion import FusedAssessment
from helix.graph import TemporalGraph
from helix.storage import Storage

from .engine import DefaultStage1Engine, EngineFactory
from .errors import UnknownCaseError
from .subgraph import Neighbourhood, RelatedSubgraph, expand, related_subgraph
from .timeline import CaseLike, ReplayFrame, ReplayRun, ReplayTimeline, frame_at, recompute, replay_timeline

CaseLookup = Callable[[str], Optional[CaseLike]]


class ReplayService:
    def __init__(
        self,
        storage: Storage,
        cases: CaseLookup | Mapping[str, CaseLike],
        cfg: HelixConfig | None = None,
        *,
        graph: TemporalGraph | None = None,
        engine_factory: EngineFactory = DefaultStage1Engine.from_snapshot,
    ) -> None:
        self.storage = storage
        self._lookup: CaseLookup = cases.get if isinstance(cases, Mapping) else cases
        self.cfg = cfg or HelixConfig()
        self.graph = graph  # live Temporal_Graph; rebuilt from Storage when None
        self.engine_factory = engine_factory

    def case(self, case_id: str) -> CaseLike:
        c = self._lookup(case_id)
        if c is None:
            raise UnknownCaseError(case_id)
        return c

    def _graph(self) -> TemporalGraph:
        if self.graph is not None:
            return self.graph
        return self.storage.rebuild_graph(self.cfg.graph.window_s)

    def subgraph(self, case_id: str) -> RelatedSubgraph:
        r = self.cfg.replay
        return related_subgraph(self.case(case_id).accounts, self._graph(), r.hops, r.max_nodes)

    def expand(self, case_id: str, account: str, t: datetime | None = None) -> Neighbourhood:
        """Neighbours of ``account`` in the live subgraph, or in the frame at ``t``."""
        if t is None:
            g = self._graph()
            r = self.cfg.replay
            sub = related_subgraph(self.case(case_id).accounts, g, r.hops, r.max_nodes)
        else:
            frame = self.at(case_id, t)
            sub = frame.subgraph
            g = self.storage.rebuild_graph(self.cfg.graph.window_s, latest_ts=t)
        return expand(sub, g, account)

    def timeline(self, case_id: str) -> ReplayTimeline:
        self.storage.flush()  # Ensure all events are written before reading (Req 24)
        return replay_timeline(self.case(case_id), self.storage, self.cfg.replay.lead_h)

    def at(self, case_id: str, t: datetime) -> ReplayFrame:
        self.storage.flush()  # Ensure all events are written before reading (Req 24)
        r = self.cfg.replay
        return frame_at(
            self.case(case_id), self.storage, t,
            window_s=self.cfg.graph.window_s, hops=r.hops, max_nodes=r.max_nodes, lead_h=r.lead_h,
        )  # fmt: skip

    def recompute_run(self, case_id: str) -> ReplayRun:
        self.storage.flush()  # Ensure all events are written before reading (Req 24)
        return recompute(self.case(case_id), self.storage, self.engine_factory, self.cfg)

    def recompute(self, case_id: str) -> list[FusedAssessment]:
        return list(self.recompute_run(case_id).assessments)


__all__ = ["CaseLookup", "ReplayService"]
