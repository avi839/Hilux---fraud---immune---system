"""SOC operations REST routes (Req 36, 37): console, handoff notes, purple team, resilience.

Mounted with ``include_ops_routes(app, state)`` on an app built by ``create_app(state)`` (same
``ApiError`` / validation error shapes as the other routes). Wiring into ``create_app`` is a
later integration step.

Routes (all under ``/api/v1``):

- ``GET  /soc/console?window_s=&time_source=wall|stream&notes_limit=``: SLA rows (open Cases,
  breached first, with the ``"SLA BREACHED"`` label and the breached timer names), workload,
  MTTD / MTTR over the window, the latest handoff notes.
- ``GET  /soc/handoff-notes?limit=``: notes, newest first. ``POST /soc/handoff-notes``
  ``{"author", "text"}`` -> 201 note; 422 ``{detail, errors: [{field, reason}]}``.
- ``POST /soc/purple/exercises`` (spec as in ``helix.soc.purple.parse_spec``) -> 201 scorecard;
  422 for an invalid spec; 409 while another exercise runs. The live state is copied on the event
  loop (no concurrent pipeline mutation) and the exercise then runs on the copy in a worker
  thread.
- ``GET  /soc/purple/scorecards`` (summaries, newest first), ``GET /soc/purple/scorecards/{id}``
  (404 unknown), ``GET /soc/purple/compare?a=&b=`` (per-metric and per-technique ``b - a``).
- ``GET  /soc/resilience``: Resilience_Mode status. ``POST /soc/resilience`` ``{"enabled": true,
  "sensor_failure_rate", "delay_rate", "burst_rate", "delay_events", "burst_size", "seed"}``
  installs the wrappers (first use) and enables injection; ``{"enabled": false}`` disables it
  and releases held events.

Components are created on first mount and stored on the state (``state.soc_console``,
``state.purple``, ``state.resilience``) unless already present, so an integrator can pre-build
them (e.g. with an Indicator_Store for the purple team).

No authentication or authorisation, like the rest of the API (prototype scope, Req 27): serve on
loopback only. Resilience_Mode deliberately degrades detection and purple exercises are CPU-heavy,
so a deployment beyond loopback needs an authenticated operator role on these routes.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, Path, Query, Request

from helix.soc.console import DEFAULT_WINDOW_S, MAX_WINDOW_S, HandoffNoteError, SocConsole
from helix.soc.purple import ExerciseSpecError, PurpleTeam
from helix.soc.resilience import ResilienceMode, rates_from_doc

from .app import API_PREFIX, ApiError, problem
from .serialize import jsonable

OPS_TAG = "soc-ops"
E404 = {404: {"description": "unknown Exercise_Scorecard"}}
E409 = {409: {"description": "another Purple_Team_Exercise is running"}}
E422 = {422: {"description": "validation failed; nothing was changed"}}
SCORECARD_ID = r"^SC-[0-9]{1,12}$"


async def _json_object(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        doc = json.loads(raw) if raw.strip() else None
    except ValueError:
        raise ApiError(422, "validation failed", errors=[problem("$", "body must be valid JSON")]) from None
    if not isinstance(doc, dict):
        raise ApiError(422, "validation failed", errors=[problem("$", "body must be a JSON object")])
    return doc


def console_for(state: Any) -> SocConsole:
    c = getattr(state, "soc_console", None)
    if c is None:
        c = SocConsole(state.helix, storage=getattr(state, "storage", None))
        state.soc_console = c
    return c


def purple_for(state: Any) -> PurpleTeam:
    p = getattr(state, "purple", None)
    if p is None:
        p = PurpleTeam(state.helix, storage=getattr(state, "storage", None),
                       indicators=lambda: getattr(state, "indicators", None),
                       attacker=lambda: getattr(state, "attacker", None))
        state.purple = p
    return p


def resilience_for(state: Any) -> ResilienceMode:
    m = getattr(state, "resilience", None)
    if m is None:
        m = ResilienceMode()
        state.resilience = m
    return m


def ops_router(state: Any) -> APIRouter:
    r = APIRouter(prefix=API_PREFIX)
    console = console_for(state)
    purple = purple_for(state)
    mode = resilience_for(state)
    running = {"exercise": False}

    # --- Req 36: console and handoff notes -----------------------------------------------

    @r.get("/soc/console", tags=[OPS_TAG], responses=E422)
    async def get_console(
        window_s: float = Query(DEFAULT_WINDOW_S, gt=0, le=MAX_WINDOW_S, description="MTTD/MTTR window, seconds"),
        time_source: Optional[str] = Query(None, pattern="^(wall|stream)$"),
        notes_limit: int = Query(20, ge=0, le=500),
    ) -> Any:
        """SLA_Timers, workload, Case ages, MTTD / MTTR and handoff notes (Req 36)."""
        return jsonable(console.snapshot(window_s=window_s, time_source=time_source, notes_limit=notes_limit))

    @r.get("/soc/handoff-notes", tags=[OPS_TAG], responses=E422)
    async def list_notes(limit: int = Query(100, ge=1, le=1000)) -> Any:
        """Shift handoff notes, newest first (Req 36.5)."""
        return {"notes": [n.to_dict() for n in console.notes.list(limit)], "total": len(console.notes)}

    @r.post("/soc/handoff-notes", status_code=201, tags=[OPS_TAG], responses=E422)
    async def add_note(request: Request) -> Any:
        """Store a shift handoff note with its author and timestamp (Req 36.5)."""
        doc = await _json_object(request)
        try:
            note = console.notes.add(doc.get("author"), doc.get("text"))
        except HandoffNoteError as e:
            raise ApiError(422, "validation failed", errors=[problem(e.field, e.reason)]) from None
        return note.to_dict()

    # --- Req 37.1-37.4: purple team ----------------------------------------------------------

    @r.post("/soc/purple/exercises", status_code=201, tags=[OPS_TAG], responses={**E409, **E422})
    async def start_exercise(request: Request) -> Any:
        """Run Attack_Campaigns against an isolated copy of the detection state and store the
        Exercise_Scorecard (Req 37.1-37.4). Live Cases, weights, pool and DNA are untouched."""
        doc = await _json_object(request)
        try:
            spec = purple.parse(doc)
        except ExerciseSpecError as e:
            raise ApiError(422, "invalid exercise", errors=[problem(f, m) for f, m in e.problems]) from None
        if running["exercise"]:
            raise ApiError(409, "another Purple_Team_Exercise is running")
        running["exercise"] = True
        try:
            ex = purple.prepare(spec)  # on the loop: the live pipeline is not mid-event
            card = await asyncio.to_thread(ex.execute)
            purple.store.save(card)
        finally:
            running["exercise"] = False
        return jsonable(card.to_dict())

    @r.get("/soc/purple/scorecards", tags=[OPS_TAG])
    async def list_scorecards() -> Any:
        """Stored Exercise_Scorecards (summaries), newest first."""
        return jsonable({"scorecards": [c.summary() for c in purple.store.list()]})

    @r.get("/soc/purple/scorecards/{scorecard_id}", tags=[OPS_TAG], responses={**E404, **E422})
    async def get_scorecard(scorecard_id: str = Path(pattern=SCORECARD_ID)) -> Any:
        try:
            return jsonable(purple.store.get(scorecard_id).to_dict())
        except KeyError:
            raise ApiError(404, f"unknown Exercise_Scorecard {scorecard_id!r}") from None

    @r.get("/soc/purple/compare", tags=[OPS_TAG], responses={**E404, **E422})
    async def compare_scorecards(a: str = Query(pattern=SCORECARD_ID), b: str = Query(pattern=SCORECARD_ID)) -> Any:
        """Difference ``b - a`` for each metric and each HELIX_Technique (Req 37.4)."""
        try:
            return jsonable(purple.compare(a, b))
        except KeyError as e:
            raise ApiError(404, f"unknown Exercise_Scorecard {e.args[0]!r}") from None

    # --- Req 37.5-37.8: resilience mode --------------------------------------------------------

    @r.get("/soc/resilience", tags=[OPS_TAG])
    async def resilience_status() -> Any:
        """Resilience_Mode status: rates, injected counts, latency percentiles while enabled, queue."""
        return jsonable(mode.status())

    @r.post("/soc/resilience", tags=[OPS_TAG], responses=E422)
    async def set_resilience(request: Request) -> Any:
        """Enable (with rates) or disable Resilience_Mode (Req 37.5)."""
        doc = await _json_object(request)
        enabled = doc.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiError(422, "validation failed", errors=[problem("enabled", "must be true or false")])
        unknown = sorted(set(doc) - {"enabled", "sensor_failure_rate", "delay_rate", "burst_rate", "delay_events",
                                     "burst_size", "seed"})
        if unknown:
            raise ApiError(422, "validation failed", errors=[problem(k, "unknown setting") for k in unknown])
        if not enabled:
            released = mode.disable()
            return jsonable({**mode.status(), "released": released})
        try:
            rates = rates_from_doc(doc)
        except ValueError as e:
            errs = [problem(p.split(" ", 1)[0], p) for p in str(e).split("; ")]
            raise ApiError(422, "validation failed", errors=errs) from None
        mode.install(state)
        mode.enable(rates)
        return jsonable(mode.status())

    return r


def include_ops_routes(app: FastAPI, state: Any) -> FastAPI:
    """Mount the SOC operations routes on ``app`` (``create_app`` already does; idempotent). Returns ``app``."""
    from .app import mount_once

    return mount_once(app, "soc_ops", lambda: ops_router(state))


__all__ = ["console_for", "include_ops_routes", "ops_router", "purple_for", "resilience_for"]
