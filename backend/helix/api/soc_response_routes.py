"""SOC response routes: Response_Playbooks (Req 32) and Canary alerts (Req 33).

Mounted with ``include_response_routes(app, state)`` on an app built by ``create_app(state)``,
so the routes reuse its error handlers (``ApiError`` -> ``{"detail", ...}``). Wiring into
``create_app`` / ``python -m helix.api`` is a later integration step.

State lookup (duck-typed, nothing in ``AppState`` is required):
- ``state.playbook_runner`` (``helix.soc.playbooks.PlaybookRunner``). When absent, one is built
  with the bundled playbooks, the state clock and ``state.audit_ledger`` (if any) as the
  audit sink, and stored back on ``state``.
- ``state.canaries`` (``helix.soc.canaries.CanaryRegistry``). When absent, one is generated
  from the generator seed (``state.generator.seed``, else ``cfg.generator.default_seed``) and
  stored back on ``state``.
- ``state.canary_campaign_of`` (optional ``event_id -> campaign_id | None``), supplied by the
  Evaluation_Monitor side; when set, ``GET /soc/canaries/hits`` adds the per-campaign report.

Routes (prefix ``/api/v1``):
- ``GET /soc/playbooks``: every loaded playbook version with an ``active`` flag, the dry-run
  mode, the Playbook_Approval_Threshold, action counts and the simulated-only notice.
- ``GET /soc/playbooks/state``: the simulated state (held tx ids, step-up and frozen accounts,
  in-memory notifications). Nothing outside HELIX is ever contacted (Req 27.11, 32.4).
- ``POST /soc/playbooks/dry-run``: JSON ``{"enabled": bool, "by": "..."}`` -> ``{"dry_run"}``.
- ``GET /soc/playbook-actions?case_id=&status=``: Playbook_Actions in scheduling order.
- ``POST /soc/playbook-actions/{action_id}/approve`` and ``/reject``: JSON ``{"by": "...",
  "reason": "..."}`` (reason optional, reject only); 404 unknown id, 409 not PENDING
  (``{"detail", "action_id", "action_status"}``).
- ``GET /soc/canaries/hits?limit=``: recorded Canary references (only touched Canary ids
  appear) and per-Canary first-reference times. The Canary list itself is never served.

No authentication or authorisation, like the rest of the API (prototype scope, Req 27):
approvals change (simulated) containment state, so a deployment beyond loopback needs an
authenticated investigator role here.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, Path, Query, Request

from helix.soc.canaries import CanaryRegistry
from helix.soc.playbooks import (
    SIMULATED_NOTICE,
    ActionNotFound,
    ActionStateError,
    ActionStatus,
    PlaybookRunner,
)

from .app import API_PREFIX, ApiError, problem
from .serialize import jsonable

SOC_TAG = "soc"
ACTION_ID_PATTERN = r"^PA-\d{6,12}$"
E404 = {404: {"description": "unknown Playbook_Action"}}
E409 = {409: {"description": "the Playbook_Action is not PENDING"}}
E422 = {422: {"description": "validation failed; nothing was changed"}}


def _clock(state: Any) -> Any:
    clk = getattr(state, "clock", None)
    return getattr(clk, "now", None)


def runner_for(state: Any) -> PlaybookRunner:
    """``state.playbook_runner``, created with the bundled playbooks when absent."""
    r = getattr(state, "playbook_runner", None)
    if r is None:
        r = PlaybookRunner.with_defaults(clock=_clock(state), audit=getattr(state, "audit_ledger", None))
        setattr(state, "playbook_runner", r)
    return r


def canaries_for(state: Any) -> CanaryRegistry:
    """``state.canaries``, generated from the generator seed when absent."""
    c = getattr(state, "canaries", None)
    if c is None:
        seed = getattr(getattr(state, "generator", None), "seed", None)
        if not isinstance(seed, int) or isinstance(seed, bool):
            cfg = getattr(state, "cfg", None)
            seed = int(getattr(getattr(cfg, "generator", None), "default_seed", 42))
        c = CanaryRegistry.generate(seed)
        setattr(state, "canaries", c)
    return c


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        raise ApiError(422, "body must be a JSON object") from None
    if not isinstance(doc, dict):
        raise ApiError(422, "body must be a JSON object")
    return doc


def _by(doc: dict[str, Any], required: bool = True) -> str:
    by = doc.get("by")
    if by is None and not required:
        return "investigator"
    if not isinstance(by, str) or not by.strip() or len(by) > 64:
        raise ApiError(422, "validation failed", errors=[problem("by", "must be a non-empty string of at most 64 "
                                                                       "characters")])
    return by


def response_router(state: Any) -> APIRouter:
    r = APIRouter(prefix=API_PREFIX)
    runner = runner_for(state)
    canaries = canaries_for(state)
    ActionId = Path(pattern=ACTION_ID_PATTERN, description="Playbook_Action identifier, e.g. PA-000001")
    by_body = {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {
                "type": "object", "required": ["by"],
                "properties": {"by": {"type": "string", "maxLength": 64},
                               "reason": {"type": "string", "maxLength": 500}}}}},
        }
    }
    dry_body = {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {
                "type": "object", "required": ["enabled"],
                "properties": {"enabled": {"type": "boolean"}, "by": {"type": "string", "maxLength": 64}}}}},
        }
    }

    # --- Req 32: playbooks ---------------------------------------------------------------

    @r.get("/soc/playbooks", tags=[SOC_TAG])
    async def list_playbooks() -> Any:
        """Loaded Response_Playbook versions, dry-run mode and approval threshold (Req 32)."""
        active = {pb.key for pb in runner.active_playbooks()}
        return jsonable({
            "playbooks": [{**pb.to_dict(), "active": pb.key in active} for pb in runner.playbooks()],
            **runner.summary(),
        })

    @r.get("/soc/playbooks/state", tags=[SOC_TAG])
    async def simulated_state() -> Any:
        """Simulated containment state; no external system is contacted (Req 32.3, 32.4, 27.11)."""
        return jsonable({**runner.state.to_dict(), "notice": SIMULATED_NOTICE})

    @r.post("/soc/playbooks/dry-run", tags=[SOC_TAG], openapi_extra=dry_body, responses={**E422})
    async def set_dry_run(request: Request) -> Any:
        """Switch dry-run mode: actions are recorded as DRY_RUN and change nothing (Req 32.6)."""
        doc = await _json_body(request)
        enabled = doc.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError(422, "validation failed", errors=[problem("enabled", "must be true or false")])
        by = _by(doc, required=False)
        return {"dry_run": runner.set_dry_run(enabled, by=by)}

    @r.get("/soc/playbook-actions", tags=[SOC_TAG], responses={**E422})
    async def list_actions(case_id: Optional[str] = Query(None, min_length=1, max_length=64),
                           status: Optional[str] = Query(None, max_length=16)) -> Any:
        """Playbook_Actions in scheduling order, optionally by Case and status (Req 23.10)."""
        if status is not None and status not in ActionStatus.__members__:
            raise ApiError(422, "validation failed", errors=[problem(
                "status", f"must be one of {', '.join(ActionStatus.__members__)}")])
        acts = runner.actions(case_id=case_id, status=status)
        return jsonable({"actions": [a.to_dict() for a in acts], "count": len(acts),
                         "dry_run": runner.dry_run, "approval_threshold": runner.approval_threshold.value,
                         "notice": SIMULATED_NOTICE})

    def _decide(action_id: str, doc: dict[str, Any], approve: bool) -> Any:
        by = _by(doc)
        reason = doc.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise ApiError(422, "validation failed", errors=[problem("reason", "must be a string of at most 500 "
                                                                               "characters")])
        try:
            a = runner.approve(action_id, by) if approve else runner.reject(action_id, by, reason)
        except ActionNotFound:
            raise ApiError(404, f"unknown Playbook_Action {action_id!r}", action_id=action_id) from None
        except ActionStateError as e:
            raise ApiError(409, str(e), action_id=action_id, action_status=e.status.value) from None
        return jsonable(a.to_dict())

    @r.post("/soc/playbook-actions/{action_id}/approve", tags=[SOC_TAG], openapi_extra=by_body,
            responses={**E404, **E409, **E422})
    async def approve_action(request: Request, action_id: str = ActionId) -> Any:
        """Approve a PENDING action: performed in simulation (or DRY_RUN in dry-run mode) (Req 32.5)."""
        return _decide(action_id, await _json_body(request), True)

    @r.post("/soc/playbook-actions/{action_id}/reject", tags=[SOC_TAG], openapi_extra=by_body,
            responses={**E404, **E409, **E422})
    async def reject_action(request: Request, action_id: str = ActionId) -> Any:
        """Reject a PENDING action; the simulated state is not changed (Req 32.5)."""
        return _decide(action_id, await _json_body(request), False)

    # --- Req 33: canary alerts -----------------------------------------------------------

    @r.get("/soc/canaries/hits", tags=[SOC_TAG], responses={**E422})
    async def canary_hits(limit: int = Query(100, ge=1, le=1000)) -> Any:
        """Canary references seen so far (most recent ``limit``) and first-reference times per
        touched Canary (Req 22.16, 33.8). Untouched Canaries are never listed."""
        doc: dict[str, Any] = {
            "hit_count": canaries.hit_count,
            "dropped_hits": canaries.dropped_hits,
            "hits": [h.to_dict() for h in canaries.hits(limit)],
            "touched": canaries.touched(),
        }
        campaign_of = getattr(state, "canary_campaign_of", None)
        if callable(campaign_of):
            doc["campaigns"] = canaries.campaign_report(campaign_of).to_dict()["campaigns"]
        return jsonable(doc)

    return r


def include_response_routes(app: FastAPI, state: Any) -> FastAPI:
    """Mount the playbook and canary routes on ``app`` (``create_app`` already does; idempotent)."""
    from .app import mount_once

    return mount_once(app, "soc_response", lambda: response_router(state))


__all__ = ["canaries_for", "include_response_routes", "response_router", "runner_for"]
