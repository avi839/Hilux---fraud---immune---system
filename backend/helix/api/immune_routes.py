"""Attacker_Agent and Immune_System routes (Req 20, 21) under ``/api/v1``.

Mounted by ``create_app`` on every app (plain and secure), so they share the API prefix,
error format and security middleware of the other routes. No authentication (prototype
scope, Req 27): serve on loopback only.

- ``POST /sim/attacker/start`` / ``POST /sim/attacker/stop`` -> ``{"running", "changed"}``;
  start attaches the Attacker_Agent on first use and answers 409 without a
  Synthetic_Generator (the attacker draws its accounts from the generator's pool).
- ``GET /attacker`` -> running flag, injection counters, genome / campaign stats and the
  most recent campaigns.
- ``GET /immune`` -> pool size and maximum, Self_Set size, miss / trigger / discard /
  retirement counts, the most recent antibodies (lineage records) and misses.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query

from . import schemas as S
from .app import API_PREFIX, ApiError
from .serialize import jsonable

RECENT = 20


def _ptype(attacker: Any, genome_id: str) -> Optional[str]:
    try:
        pt = attacker.genome(genome_id).ptype
    except KeyError:
        return None
    return str(getattr(pt, "value", pt))


def campaign_doc(attacker: Any, c: Any) -> dict[str, Any]:
    return {
        "campaign_id": c.campaign_id, "genome_id": c.genome_id, "pattern_type": _ptype(attacker, c.genome_id),
        "started_at": c.started_at, "events": len(c.event_ids), "injected": len(c.injected_ids),
        "withheld": len(c.withheld_ids), "evasion": c.evasion, "completed": c.completed,
        "window_end": c.window_end,
    }


def genome_doc(g: Any, agent: Any = None) -> dict[str, Any]:
    """One Pattern_Genome: identity, lineage, score, its numeric ``params`` and, when the parent
    is known to ``agent``, ``changed_dimensions`` (the Req 20.7 dimensions that differ from the
    parent; None for a root genome or an unknown parent)."""
    params = {str(k): (int(v) if isinstance(v, int) and not isinstance(v, bool) else float(v))
              for k, v in dict(getattr(g, "params", None) or {}).items()}
    changed: Optional[list[str]] = None
    if agent is not None and g.parent_id is not None:
        try:
            parent = agent.genome(g.parent_id)
            changed = [str(d) for d in g.differs_from(parent)]
        except (KeyError, AttributeError):
            changed = None
    return {"genome_id": g.genome_id, "pattern_type": str(getattr(g.ptype, "value", g.ptype)),
            "parent_id": g.parent_id, "generation": g.generation, "evasion": g.evasion,
            "scored_campaigns": g.scored_campaigns, "retired": g.retired, "params": params,
            "changed_dimensions": changed}


def attacker_doc(state: Any, recent: int = RECENT) -> dict[str, Any]:
    """``GET /attacker`` body; the same keys whether or not the attacker is attached."""
    a = state.attacker
    doc: dict[str, Any] = {
        "configured": a is not None, "available": a is not None or state.generator is not None,
        "running": state.attacker_running(), "seed": None, "injected_total": 0, "injected_tx_total": 0,
        "withheld_total": 0, "deferred": 0, "pending_events": 0, "genomes_total": 0, "genomes_active": 0,
        "campaigns_total": 0, "campaigns_completed": 0, "campaigns_live": 0, "mean_evasion": None,
        "recent_campaigns": [], "top_genomes": [],
    }
    if a is None:
        return doc
    agent = a.agent
    camps = agent.campaigns()
    done = [c for c in camps if c.completed]
    ev = [c.evasion for c in done if c.evasion is not None]
    scored = sorted((g for g in agent.active_genomes() if g.evasion is not None),
                    key=lambda g: (-g.evasion, g.genome_id))
    doc.update(
        seed=agent.seed, injected_total=agent.injected_total, injected_tx_total=agent.injected_tx_total,
        withheld_total=agent.withheld_total, deferred=agent.deferred, pending_events=agent.pending_events,
        genomes_total=len(agent.genomes()), genomes_active=len(agent.active_genomes()),
        campaigns_total=len(camps), campaigns_completed=len(done), campaigns_live=len(camps) - len(done),
        mean_evasion=(sum(ev) / len(ev)) if ev else None,
        recent_campaigns=[campaign_doc(agent, c) for c in sorted(camps, key=lambda c: c.started_at)[-recent:][::-1]],
        top_genomes=[genome_doc(g, agent) for g in scored[:10]],
    )
    return jsonable(doc)


def immune_doc(state: Any, recent: int = RECENT) -> dict[str, Any]:
    """``GET /immune`` body; the same keys whether or not the Immune_System is enabled."""
    imm = state.immune
    det = state.miss_detector
    icfg = state.cfg.immune if imm is None else imm.cfg
    doc: dict[str, Any] = {
        "enabled": imm is not None, "miss_detection": det is not None, "pool_size": 0,
        "pool_max": int(icfg.pool_max), "held": 0, "self_set_size": 0, "self_set_min": int(icfg.self_set_min),
        "detection_deadline_s": float(icfg.detection_deadline_s), "misses": 0, "pending_attack_events": 0,
        "triggers": 0, "discards": 0, "retirements": 0, "by_status": {}, "recent_antibodies": [],
        "recent_misses": [],
    }
    if imm is None:
        return doc
    s = imm.summary()
    lineage = imm.lineage()  # creation order
    doc.update(
        pool_size=s["active"], held=s["held"], self_set_size=s["self_set_size"], triggers=s["triggers"],
        discards=s["discards"], retirements=s["retirements"], by_status=s["by_status"],
        recent_antibodies=lineage[-recent:][::-1],  # lineage records, newest first
    )
    if det is not None:
        doc.update(misses=len(det.misses), pending_attack_events=det.pending,
                   recent_misses=[{"event_id": m.event_id, "campaign_id": m.campaign_id, "miss_time": m.miss_time}
                                  for m in det.misses[-recent:][::-1]])
    return jsonable(doc)


def immune_router(state: Any) -> APIRouter:
    r = APIRouter(prefix=API_PREFIX)
    no_gen = {409: {"model": S.ErrorBody, "description": "no Synthetic_Generator configured"}}
    Recent = Query(RECENT, ge=1, le=200, description="number of recent items, 1-200")

    @r.post("/sim/attacker/start", response_model=S.GeneratorState, tags=["sim"], responses=no_gen)
    async def start_attacker() -> Any:
        """Start the Attacker_Agent (attached on first use) (Req 20)."""
        try:
            changed = state.start_attacker()
        except LookupError as exc:
            raise ApiError(409, str(exc)) from None
        return {"running": True, "changed": changed}

    @r.post("/sim/attacker/stop", response_model=S.GeneratorState, tags=["sim"])
    async def stop_attacker() -> Any:
        """Stop the Attacker_Agent's tick task; its genomes and campaigns are kept."""
        return {"running": False, "changed": state.stop_attacker()}

    @r.get("/attacker", response_model=S.Document, tags=["sim"])
    async def get_attacker(recent: int = Recent) -> Any:
        return attacker_doc(state, recent)

    @r.get("/immune", response_model=S.Document, tags=["learn"])
    async def get_immune(recent: int = Recent) -> Any:
        return immune_doc(state, recent)

    return r


__all__ = ["attacker_doc", "immune_doc", "immune_router"]
