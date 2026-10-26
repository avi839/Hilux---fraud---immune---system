"""FastAPI API_Server (design §21): REST under ``/api/v1`` plus the WebSocket hub.

No authentication or authorisation is implemented (prototype scope, Req 27); serve on
loopback only. ``secure_app(state)`` (= ``create_app(state, secure=True)``, used by
``python -m helix.api``) installs the controls in ``security.py``: exact-origin CORS,
security headers, a request body limit and the WebSocket Origin guard. ``create_app`` also
has two extension slots:

- ``middleware``: extra Starlette ``Middleware`` entries, installed inside the security ones;
- ``ws_guard``: an async callable run for every WebSocket handshake *before* ``accept()``;
  returning False closes the socket with 1008 (policy violation).

Every handler validates its input before it touches HELIX state, so a 4xx response leaves
all state unchanged (Req 23.4, 23.5, 23.9).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import APIRouter, Body, FastAPI, Path, Query, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from starlette.middleware import Middleware
from starlette.websockets import WebSocketDisconnect, WebSocketState

from helix.cases import Case, CaseConflict, CaseNotFound, CaseValidationError
from helix.codec import TransactionEvent
from helix.config import ConfigLoadError, HelixConfig, range_paths, with_updates
from helix.core import CASES, EVENTS, METRICS
from helix.explain import dumps as chain_dumps
from helix.explain import explain
from helix.ingest import IngestStatus
from helix.replay import (
    ReplayParameterError,
    TimestampOutOfRangeError,
    UnknownAccountError,
    UnknownCaseError,
    expand,
    related_subgraph,
)
from helix.risk import risk as risk_score
from helix.risk import tier as _tier

from . import schemas as S
from .hub import Client
from .security import (
    DEFAULT_MAX_BODY_BYTES,
    WSGuard,
    chain_guards,
    make_ws_origin_guard,
    security_middleware,
    security_stats,
)
from .serialize import case_detail, decode_cursor, dumps, explanation_doc, jsonable, paginate
from .state import AppState

API_PREFIX = "/api/v1"
API_VERSION = "1.0.0"
WS_CHANNELS = (EVENTS, CASES, METRICS)
NO_CURRENCY = "XXX"  # ISO 4217 "no currency": an amount whose currency is not cached

E404 = {404: {"model": S.ErrorBody, "description": "unknown Case"}}
E409 = {409: {"model": S.ErrorBody, "description": "workflow conflict (wrong status or assignee)"}}
E422 = {422: {"model": S.ValidationErrorBody, "description": "validation failed; nothing was changed"}}
E503 = {503: {"model": S.ErrorBody, "description": "requires Storage, which is not configured"}}

RUNTIME_RANGES = {p: r for p, r in range_paths(HelixConfig).items() if r.runtime}
log = logging.getLogger(__name__)


# --- errors -------------------------------------------------------------------------------


class ApiError(Exception):
    """Raised by handlers; rendered as ``{"detail": ..., **extra}`` with ``status``."""

    def __init__(self, status: int, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status, self.detail, self.extra = status, detail, extra


def problem(field: str, reason: str, rule: Optional[str] = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"field": field, "reason": reason}
    if rule is not None:
        out["rule"] = rule
    out.update(extra)
    return out


def invalid(errors: Sequence[Mapping[str, Any]], detail: str = "validation failed", **extra: Any) -> ApiError:
    return ApiError(422, detail, errors=list(errors), **extra)


def _loc(loc: Sequence[Any], kind: str) -> str:
    parts = [str(p) for p in loc]
    if parts and parts[0] in ("body", "query", "path", "header", "cookie"):
        parts = parts[1:]
    if kind == "json_invalid" or not parts:
        return "$"
    return ".".join(parts)


def _install_error_handlers(app: FastAPI) -> None:
    def body(detail: str, **extra: Any) -> dict[str, Any]:
        return {"detail": detail, **jsonable(extra)}

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [problem(_loc(e.get("loc", ()), e.get("type", "")), e.get("msg", "invalid"), e.get("type"))
                for e in exc.errors()]
        return JSONResponse(body("validation failed", errors=errs), status_code=422)

    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(body(exc.detail, **exc.extra), status_code=exc.status)

    @app.exception_handler(CaseNotFound)
    async def _not_found(_: Request, exc: CaseNotFound) -> JSONResponse:
        return JSONResponse(body(f"unknown case id {exc.case_id!r}", case_id=exc.case_id), status_code=404)

    @app.exception_handler(UnknownCaseError)
    async def _unknown_case(_: Request, exc: UnknownCaseError) -> JSONResponse:
        return JSONResponse(body(str(exc), case_id=exc.case_id), status_code=404)

    @app.exception_handler(CaseConflict)
    async def _conflict(_: Request, exc: CaseConflict) -> JSONResponse:
        return JSONResponse(body(exc.reason, case_id=exc.case_id, assignee=exc.assignee), status_code=409)

    @app.exception_handler(CaseValidationError)
    async def _case_invalid(_: Request, exc: CaseValidationError) -> JSONResponse:
        return JSONResponse(body("validation failed", case_id=exc.case_id, errors=[problem("$", exc.reason)]),
                            status_code=422)

    @app.exception_handler(UnknownAccountError)
    async def _unknown_account(_: Request, exc: UnknownAccountError) -> JSONResponse:
        return JSONResponse(body(str(exc), account=exc.account), status_code=404)

    @app.exception_handler(TimestampOutOfRangeError)
    async def _t_range(_: Request, exc: TimestampOutOfRangeError) -> JSONResponse:
        errs = [problem("t", str(exc), "out_of_range")]
        return JSONResponse(body("validation failed", errors=errs, start=exc.start, end=exc.end), status_code=422)

    @app.exception_handler(ReplayParameterError)
    async def _replay_param(_: Request, exc: ReplayParameterError) -> JSONResponse:
        return JSONResponse(body("validation failed", errors=[problem(exc.parameter, str(exc), "out_of_range")]),
                            status_code=422)


# --- helpers ------------------------------------------------------------------------------


def _currencies(state: AppState, case: Case) -> dict[str, str]:
    """tx_id -> currency for every amount of the Case (Explanation_Engine summary line)."""
    out: dict[str, str] = {}
    for m in case.motifs.values():
        for e in m.edges:
            out.setdefault(e.tx_id, e.currency)
    info = getattr(state.helix, "_info", None)
    for tx in case.amounts:
        i = info(tx) if info is not None else None
        if i is not None:
            out[tx] = i.ev.currency
        out.setdefault(tx, NO_CURRENCY)
    return out


def effective_weights(helix: Any) -> dict[str, float]:
    """Sensor_Weight in effect for every stage-1 sensor (1.0 unless learned or rolled back),
    plus any other weighted contributor the pipeline holds a weight for; sorted by name."""
    w = dict(helix.weights)
    names = {str(getattr(s, "name", "")) for s in getattr(helix.stage1, "sensors", ())} - {""}
    return {k: float(w.get(k, 1.0)) for k in sorted(names | set(w))}


def _flatten(doc: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in doc.items():
        path = f"{prefix}{k}"
        if isinstance(v, Mapping):
            out.update(_flatten(v, path + "."))
        else:
            out[path] = v
    return out


def _permitted(path: str) -> dict[str, Any]:
    r = RUNTIME_RANGES[path]
    return {"lo": r.lo, "hi": r.hi, "lo_open": r.lo_open, "rule": r.describe()}


def check_runtime_patch(cfg: HelixConfig, doc: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate a PATCH /config document against the runtime settings. Returns the dotted
    updates and every problem found (all-or-nothing: apply only when there are none)."""
    updates = _flatten(doc)
    known = set(_flatten(cfg.model_dump()))
    errs: list[dict[str, Any]] = []
    if not updates:
        errs.append(problem("$", "at least one setting is required", "missing"))
    runtime = sorted(RUNTIME_RANGES)
    for path, value in sorted(updates.items()):
        if path not in known:
            errs.append(problem(path, "unknown setting", "extra_forbidden", runtime_settings=runtime))
            continue
        rng = RUNTIME_RANGES.get(path)
        if rng is None:
            errs.append(problem(path, "not changeable at runtime", "not_runtime", runtime_settings=runtime))
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errs.append(problem(path, "must be a number", "number_type", permitted=_permitted(path)))
            continue
        integer = path == "prioritiser.alert_budget"
        if integer and isinstance(value, float) and not value.is_integer():
            errs.append(problem(path, "must be an integer", "int_type", permitted=_permitted(path)))
            continue
        if not rng.contains(float(value)):
            errs.append(problem(path, rng.describe(), "out_of_range", permitted=_permitted(path)))
            continue
        if integer:
            updates[path] = int(value)
    return updates, errs


# --- app factory ----------------------------------------------------------------------------


def create_app(
    state: AppState,
    *,
    middleware: Sequence[Middleware] = (),
    ws_guard: Optional[WSGuard] = None,
    manage_lifecycle: bool = True,
    secure: bool = False,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> FastAPI:
    """Build the API app over ``state``. With ``manage_lifecycle`` the app lifespan starts
    and stops the pipeline worker, Storage writer/snapshotter, metrics ticker and (when
    autostart is set) the generator feeder.

    With ``secure`` the security controls are installed (outermost) for the configured
    ``api.dashboard_origin``, and the WebSocket Origin guard runs before ``ws_guard``.
    Production serving goes through ``secure_app``; the bare app is for in-process tests."""
    if secure:
        origin = state.cfg.api.dashboard_origin
        middleware = [*security_middleware(state, origin, max_body_bytes), *middleware]
        ws_guard = chain_guards(make_ws_origin_guard(state, origin), ws_guard)

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        if manage_lifecycle:
            await state.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                await state.stop()

    app = FastAPI(
        title="HELIX API",
        version=API_VERSION,
        description="HELIX fraud immune system API. No authentication: serve on loopback only.",
        lifespan=lifespan,
        middleware=list(middleware),
    )
    app.state.helix = state
    _install_error_handlers(app)
    app.include_router(_router(state))
    from .immune_routes import immune_router  # imports API_PREFIX/ApiError from this module

    app.include_router(immune_router(state))
    _include_soc_routers(app, state)
    from .dataset_routes import include_dataset_routes  # Req 39 (design §24)
    include_dataset_routes(app, state)
    _install_websockets(app, state, ws_guard)
    app.state.secure = secure
    return app


def mount_once(app: FastAPI, key: str, build: Callable[[], APIRouter]) -> FastAPI:
    """Include the router from ``build()`` unless a router under ``key`` is already mounted
    on ``app`` (the ``include_*_routes`` helpers are idempotent on a ``create_app`` app)."""
    mounted: set[str] = getattr(app.state, "mounted_routers", None) or set()
    if key not in mounted:
        app.include_router(build())
        mounted.add(key)
        app.state.mounted_routers = mounted
    return app


def _include_soc_routers(app: FastAPI, state: AppState) -> None:
    """Mount every SOC router (Req 28-37). Each one works with cyber fusion off too: the
    Auth_Event route answers 503 and the components fall back to in-memory defaults. Intel
    goes first so the Audit_Ledger exists before the Playbook_Runner looks it up."""
    from .soc_intel_routes import include_intel_routes
    from .soc_ops_routes import include_ops_routes
    from .soc_response_routes import include_response_routes
    from .soc_routes import include_soc_routes

    for include in (include_soc_routes, include_intel_routes, include_response_routes, include_ops_routes):
        include(app, state)


def secure_app(
    state: AppState,
    *,
    allow_missing_ws_origin: Optional[bool] = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    manage_lifecycle: bool = True,
) -> FastAPI:
    """The production app: ``create_app(state, secure=True)``. ``allow_missing_ws_origin``,
    when given, sets ``state.allow_missing_ws_origin`` (default False: a WebSocket handshake
    without an Origin header is rejected)."""
    if allow_missing_ws_origin is not None:
        state.allow_missing_ws_origin = bool(allow_missing_ws_origin)
    return create_app(state, secure=True, max_body_bytes=max_body_bytes, manage_lifecycle=manage_lifecycle)


def _router(state: AppState) -> APIRouter:  # noqa: C901 - one closure per route
    r = APIRouter(prefix=API_PREFIX)
    helix = state.helix
    CaseId = Path(min_length=1, max_length=64, description="Case identifier, e.g. C-12")

    def get_case(case_id: str) -> Case:
        return helix.cases.get(case_id)  # CaseNotFound -> 404

    def detail(case: Case) -> dict[str, Any]:
        return case_detail(helix.case_summary(case), case, helix.chain(case.case_id) is not None)

    def need_replay() -> Any:
        if state.replay is None:
            raise ApiError(503, "replay timeline requires Storage, which is not configured")
        return state.replay

    # ingest ------------------------------------------------------------------------------

    event_body = {
        "requestBody": {
            "required": True,
            "description": "one Transaction_Event JSON document; undefined keys are ignored",
            "content": {"application/json": {"schema": TransactionEvent.model_json_schema()}},
        }
    }

    @r.post("/events", status_code=202, response_model=S.IngestAccepted, tags=["ingest"],
            responses={**E422, 202: {"description": "accepted or duplicate; X-Backpressure header set",
                                     "headers": {"X-Backpressure": {"schema": {"type": "string"}}}}},
            openapi_extra=event_body)
    async def ingest_event(request: Request, response: Response) -> Any:
        """Ingest one Transaction_Event (Req 23.1, 23.6). Duplicates are acknowledged, not re-admitted."""
        res = state.ingestor.submit(await request.body())
        bp = "true" if res.backpressure else "false"
        if res.status is IngestStatus.REJECTED:
            errs = [problem(e.field, e.message, e.rule) for e in res.error.errors]  # type: ignore[union-attr]
            return JSONResponse({"detail": "invalid Transaction_Event", "errors": errs}, status_code=422,
                                headers={"X-Backpressure": bp})
        response.headers["X-Backpressure"] = bp
        dup = res.status is IngestStatus.DUPLICATE
        return S.IngestAccepted(tx_id=res.tx_id or "", status=res.status.value, duplicate=dup,
                                backpressure=res.backpressure)

    # cases ------------------------------------------------------------------------------

    @r.get("/cases", response_model=S.CasePage, tags=["cases"], responses=E422)
    async def list_cases(
        page_size: Optional[int] = Query(None, ge=1, le=100, description="1-100, default 50"),
        cursor: Optional[str] = Query(None, description="next_cursor of the previous page"),
        page_token: Optional[str] = Query(None, description="alias of cursor"),
    ) -> Any:
        """Cases in creation order, one page at a time (Req 23.8)."""
        token = cursor if cursor is not None else page_token
        after = None
        if token is not None:
            after = decode_cursor(token)
            if after is None:
                raise invalid([problem("cursor", "not a page token issued by this server", "token_invalid")])
        size = page_size if page_size is not None else state.cfg.api.page_size
        cases = helix.cases.cases
        page, nxt = paginate(cases.values(), size, after)
        return {"items": [helix.case_summary(c) for c in page], "page_size": size, "total": len(cases),
                "next_cursor": nxt, "backlog_count": len(helix.prioritiser.backlog_ids())}

    @r.get("/cases/{case_id}", response_model=S.CaseDetail, tags=["cases"], responses={**E404, **E422})
    async def get_case_detail(case_id: str = CaseId) -> Any:
        return detail(get_case(case_id))

    @r.get("/cases/{case_id}/evidence", response_model=S.EvidenceResponse, tags=["cases"],
           responses={**E404, **E422})
    async def get_evidence(case_id: str = CaseId) -> Any:
        """Evidence_Chain plus its explanation (summary, reasons, mitigating, insufficient, notes)."""
        case = get_case(case_id)
        chain = helix.chain(case_id)
        ex = explain(case, chain, currency=_currencies(state, case))
        return {"case_id": case_id, "chain": None if chain is None else json.loads(chain_dumps(chain)),
                "explanation": explanation_doc(ex)}

    @r.get("/cases/{case_id}/subgraph", response_model=S.Document, tags=["replay"], responses={**E404, **E422})
    async def get_subgraph(case_id: str = CaseId,
                           hops: Optional[int] = Query(None, ge=1, le=4, description="1-4, default replay.hops")) -> Any:
        """Related_Subgraph of the Case accounts in the live Graph_Window (Req 17.1, 17.3)."""
        case = get_case(case_id)
        rc = state.cfg.replay
        sub = related_subgraph(case.accounts, helix.graph, hops if hops is not None else rc.hops, rc.max_nodes)
        return {"case_id": case_id, **jsonable(sub)}

    async def _expand(case_id: str, account: str, t: Optional[Any]) -> dict[str, Any]:
        case = get_case(case_id)
        if t is None:
            rc = state.cfg.replay
            sub = related_subgraph(case.accounts, helix.graph, rc.hops, rc.max_nodes)
            nb = expand(sub, helix.graph, account)
        else:
            nb = need_replay().expand(case_id, account, t)
        return {"case_id": case_id, **jsonable(nb)}

    @r.post("/cases/{case_id}/expand", response_model=S.Document, tags=["replay"],
            responses={404: {"model": S.ErrorBody, "description": "unknown Case or account not in the subgraph"},
                       **E422, **E503})
    async def expand_account(req: S.ExpandRequest, case_id: str = CaseId) -> Any:
        """Direct neighbours and connecting edges of one subgraph account (Req 17.2, 17.6)."""
        return await _expand(case_id, req.account, req.t)

    @r.get("/cases/{case_id}/subgraph/{account}/neighbours", response_model=S.Document, tags=["replay"],
           responses={404: {"model": S.ErrorBody, "description": "unknown Case or account not in the subgraph"},
                      **E422, **E503})
    async def neighbours(case_id: str = CaseId, account: str = Path(min_length=1, max_length=64),
                         t: Optional[AwareDatetime] = Query(None)) -> Any:
        return await _expand(case_id, account, t)

    @r.get("/cases/{case_id}/timeline", response_model=S.Document, tags=["replay"],
           responses={**E404, **E422, **E503})
    @r.get("/cases/{case_id}/replay", response_model=S.Document, tags=["replay"],
           responses={**E404, **E422, **E503})
    async def get_timeline(case_id: str = CaseId) -> Any:
        """Replay_Timeline: lead time before the first contributing event to the last update (Req 17.4)."""
        get_case(case_id)
        tl = need_replay().timeline(case_id)
        return jsonable(tl)

    @r.get("/cases/{case_id}/timeline/at", response_model=S.Document, tags=["replay"],
           responses={**E404, 422: {"model": S.ReplayRangeErrorBody, "description": "t outside the timeline"},
                      **E503})
    @r.get("/cases/{case_id}/replay/at", response_model=S.Document, tags=["replay"],
           responses={**E404, 422: {"model": S.ReplayRangeErrorBody, "description": "t outside the timeline"},
                      **E503})
    async def get_frame(case_id: str = CaseId, t: AwareDatetime = Query(..., description="ISO 8601 with offset")) -> Any:
        """Point-in-time frame at t: subgraph over [t - Graph_Window, t] and assessments at or before t (Req 17.5, 17.7)."""
        get_case(case_id)
        frame = need_replay().at(case_id, t)
        return jsonable(frame)

    # workflow ---------------------------------------------------------------------------

    @r.post("/cases/{case_id}/open", response_model=S.CaseDetail, tags=["workflow"],
            responses={**E404, **E409, **E422})
    async def open_case(req: S.OpenRequest, case_id: str = CaseId) -> Any:
        """Claim a Case: OPEN -> IN_REVIEW with the investigator as assignee (Req 18.2, 18.6)."""
        return detail(helix.open_case(case_id, req.investigator))

    @r.post("/cases/{case_id}/feedback", response_model=S.CaseDetail, tags=["workflow"],
            responses={**E404, **E409, **E422})
    async def submit_feedback(req: S.FeedbackRequest, case_id: str = CaseId) -> Any:
        """Feedback_Label by the assignee: closes the Case and drives LEARN (Req 18.3, 18.7)."""
        return detail(helix.label(case_id, req.by, req.label, req.note))

    @r.post("/cases/{case_id}/escalate", response_model=S.CaseDetail, tags=["workflow"],
            responses={**E404, **E409, **E422})
    async def escalate_case(req: S.EscalateRequest, case_id: str = CaseId) -> Any:
        """Escalate to CRITICAL (Req 18.4, 18.8)."""
        return detail(helix.escalate(case_id, req.by))

    @r.post("/cases/bulk-feedback", response_model=S.BulkFeedbackResult, tags=["workflow"],
            responses={404: {"model": S.BulkFailureBody, "description": "an unknown Case; nothing changed"},
                       409: {"model": S.BulkFailureBody, "description": "a CLOSED or foreign-assignee Case; "
                                                                        "nothing changed"},
                       422: {"model": S.BulkFailureBody, "description": "0 or more than 50 ids, duplicates, "
                                                                        "or an invalid body; nothing changed"},
                       500: {"model": S.ErrorBody, "description": "internal error while applying"}})
    async def bulk_feedback(req: S.BulkFeedbackRequest) -> Any:
        """One Feedback_Label on 1-50 distinct Cases, all or nothing (Req 41.6, 41.7). Every Case
        is checked against the Requirement 18 rules first (an OPEN unassigned Case counts as
        opened by ``by``); on any failure the most severe status (422 > 404 > 409) is returned
        with every failing id and nothing changes. Each Case is then a normal Feedback_Label."""
        from helix.cases import BulkFeedbackError

        try:
            done = helix.label_bulk(req.by, req.label, req.note, req.case_ids)
        except BulkFeedbackError as exc:
            return JSONResponse({"detail": exc.detail, "failures": [f.to_dict() for f in exc.failures]},
                                status_code=exc.status)
        except Exception:  # noqa: BLE001 - phase 2 cannot fail on a validated batch: a bug
            log.exception("bulk feedback failed after validation")
            return JSONResponse({"detail": "internal error while applying bulk feedback"}, status_code=500)
        state.enable_workload().on_bulk_request()
        return {"by": req.by, "label": req.label.value, "count": len(done), "case_ids": [c.case_id for c in done],
                "cases": [helix.case_summary(c) for c in done]}

    # config -----------------------------------------------------------------------------

    def config_doc() -> dict[str, Any]:
        pr = helix.prioritiser
        return {
            "config": state.cfg.model_dump(mode="json"),
            "runtime": {p: _permitted(p) for p in sorted(RUNTIME_RANGES)},
            "effective": {"prioritiser.alert_budget": pr.alert_budget,
                          "prioritiser.investigation_cost": float(pr.investigation_cost)},
            "pending": dict(pr.pending()),
            "current_period": pr.current_period,
        }

    @r.get("/config", response_model=S.Document, tags=["config"])
    async def get_config() -> Any:
        return config_doc()

    @r.patch("/config", response_model=S.ConfigPatchResult, tags=["config"],
             responses={422: {"model": S.ValidationErrorBody,
                              "description": "unknown, non-runtime or out-of-range setting; nothing changed"}})
    async def patch_config(doc: dict[str, Any] = Body(..., examples=[{"prioritiser.alert_budget": 30}])) -> Any:
        """Change runtime settings (Alert_Budget, Investigation_Cost), all or nothing.
        Accepts dotted paths or nested sections; applies from the next budget period (Req 14.10, 14.11)."""
        updates, errs = check_runtime_patch(state.cfg, doc)
        if errs:
            raise invalid(errs)
        try:
            candidate = with_updates(state.cfg, updates)
        except ConfigLoadError as exc:
            raise invalid([problem(e.path, e.rule) for e in exc.errors]) from None
        cross = candidate.validate()
        if cross:
            raise invalid([problem(e.path, e.rule, "config_invalid") for e in cross])
        state.apply_runtime_config(candidate, updates)
        pr = helix.prioritiser
        return {"accepted": updates, "applies_from_period": pr.current_period + 1, "pending": dict(pr.pending())}

    # metrics ----------------------------------------------------------------------------

    def latency_doc() -> dict[str, Any]:
        rec = helix.latency
        rep = rec.report()
        return {"window_s": rec.window_s, "window": rep["window"], "since_start": rep["since_start"],
                "ingest_rate_eps": state.ingest_rate(), "queue_length": len(state.ingestor),
                "latency_breach": state.breach()}

    @r.get("/metrics", response_model=S.Document, tags=["metrics"])
    async def get_metrics() -> Any:
        """Loop, ingest, latency and Evaluation_Monitor metrics (Req 23.1), plus the current
        Sensor_Weights used by stage-1 fusion (``weights``: sensor name -> weight)."""
        mon = state.monitor
        return {
            "weights": effective_weights(helix),
            "loop": jsonable(helix.metrics()),
            "stages": state.stage_means(),
            "ingest": state.ingest_stats(),
            "latency_breach": state.breach(),
            "evaluation": None if mon is None else jsonable(mon.metrics()),
            "ws": state.hub.stats(),
            "security": security_stats(state),
            "workload": state.workload_metrics(),  # Req 41.10
        }

    @r.get("/latency", response_model=S.Document, tags=["metrics"])
    @r.get("/metrics/latency", response_model=S.Document, tags=["metrics"])
    async def get_latency() -> Any:
        """p50/p95/p99 per stage and end to end, 60 s rolling and since start, with counts;
        "unavailable" when a window has no samples (Req 25.5, 25.6, 25.7)."""
        return latency_doc()

    # learning ---------------------------------------------------------------------------

    @r.post("/weights/rollback", response_model=S.RollbackResult, tags=["learn"],
            responses={422: {"model": S.ReplayRangeErrorBody, "description": "t outside the weight history"},
                       501: {"model": S.ErrorBody, "description": "Feedback_Learner not available"}})
    async def rollback_weights(req: S.RollbackRequest) -> Any:
        """Restore every Sensor_Weight to its value at time t (Req 19.9, 19.10)."""
        learner = state.learner
        if learner is None:
            raise ApiError(501, "Feedback_Learner is not available; weight rollback is disabled")
        from helix.learn import RollbackRangeError

        try:
            recs = learner.rollback(req.t)
        except RollbackRangeError as exc:
            raise invalid([problem("t", str(exc), "out_of_range")], start=exc.earliest, end=exc.latest) from None
        return {"t": req.t.isoformat(), "records": [jsonable(x) for x in recs], "weights": dict(helix.weights)}

    # simulation -------------------------------------------------------------------------

    gen_errors = {409: {"model": S.ErrorBody, "description": "no Synthetic_Generator configured"}}

    @r.post("/sim/generator/start", response_model=S.GeneratorState, tags=["sim"], responses=gen_errors)
    async def start_generator() -> Any:
        if state.generator is None:
            raise ApiError(409, "no Synthetic_Generator configured")
        changed = state.start_generator()
        return {"running": True, "changed": changed}

    @r.post("/sim/generator/stop", response_model=S.GeneratorState, tags=["sim"], responses=gen_errors)
    async def stop_generator() -> Any:
        if state.generator is None:
            raise ApiError(409, "no Synthetic_Generator configured")
        changed = state.stop_generator()
        return {"running": False, "changed": changed}

    # status -----------------------------------------------------------------------------

    @r.get("/status", response_model=S.Document, tags=["status"])
    async def get_status() -> Any:
        """Health: worker, storage-degraded flag (Req 24.10), latency breach (Req 25.7)."""
        st = state.storage.status() if state.storage is not None else None
        degraded = bool(st is not None and st.degraded)
        breach = state.breach()
        return {
            "status": "storage_degraded" if degraded else "ok",
            "uptime_s": None if state.started_at is None else time.monotonic() - state.started_at,
            "worker": {"running": state.running("worker"), "errors": state.worker_errors,
                       "processed": helix.seq_no},
            "storage": None if st is None else {"configured": True, **jsonable(st)},
            "generator": {"configured": state.generator is not None, "running": state.running("generator")},
            "attacker": {"configured": state.attacker is not None, "running": state.attacker_running()},
            "immune": {"enabled": state.immune is not None, "miss_detection": state.miss_detector is not None,
                       "running": state.running("immune")},
            "learner": state.learner is not None,
            "replay": state.replay is not None,
            "ingest": state.ingest_stats(),
            "latency_breach": breach,
            "ws": state.hub.stats(),
            "security": security_stats(state),
        }

    # transaction trace ------------------------------------------------------------------

    def _mass_doc(m: Any) -> dict[str, float]:
        return {"fraud": float(m.f), "legit": float(m.l), "uncertainty": float(m.t)}

    def _assessment_doc(fa: Any) -> dict[str, Any]:
        return {"belief": float(fa.belief), "plausibility": float(fa.plausibility),
                "uncertainty": float(fa.uncertainty), "conflict": float(fa.conflict),
                "high_conflict": bool(fa.high_conflict)}

    def _motif_hits(fa: Any) -> list[dict[str, Any]]:
        return [{"contributor_id": c.contributor_id, "mass": _mass_doc(c.mass), "weight": float(c.weight)}
                for c in fa.contributions if c.kind == "MOTIF"]

    def _act_learn(tx_id: str, stage2: Any, risk_value: Optional[float]) -> tuple[dict[str, Any], dict[str, Any]]:
        score = float(risk_value) if risk_value is not None else float(risk_score(stage2))
        case_id = helix.cases.case_of(tx_id)
        case = helix.cases.cases.get(case_id) if case_id else None
        act = {"risk_score": score, "severity_tier": jsonable(_tier(score, helix.cfg.risk.bands)), "case_id": case_id}
        learn = {"label": jsonable(case.label) if case is not None else None,
                 "note": "Feedback_Labels recorded on the Case feed the Weight_Learner and the Immune_System."}
        return act, learn

    @r.get("/trace/{tx_id}", response_model=S.Document, tags=["trace"],
           responses={404: {"model": S.ErrorBody, "description": "unknown Transaction_Event identifier"}})
    async def get_transaction_trace(tx_id: str = Path(min_length=1, max_length=64,
                                                      description="Transaction_Event identifier")) -> Any:
        """Transaction_Event path through SENSE, PERCEIVE, REMEMBER, REASON, ACT and LEARN
        (Req 38.4); an unknown identifier is 404 (Req 38.5). Events still held by the pipeline
        (recent window or Case members) give the full trace; otherwise the stored assessments
        are used when Storage is configured (no per-sensor detail)."""
        info = helix._info(tx_id)
        if info is not None:
            ev = info.ev
            act, learn = _act_learn(tx_id, info.stage2, None)
            return {
                "tx_id": tx_id,
                "timestamp": ev.ts.isoformat(),
                "source": "pipeline",
                "event": jsonable({"src": ev.src, "dst": ev.dst, "amount": ev.amount, "currency": ev.currency,
                                   "channel": ev.channel, "mcc": ev.mcc, "lat": ev.lat, "lon": ev.lon,
                                   "device_id": ev.device_id, "session_id": ev.session_id}),
                "stages": {
                    "SENSE": {
                        "received_at": info.received_at.isoformat(),
                        "lateness": {"kind": jsonable(info.lateness.kind), "seconds": float(info.lateness.seconds)},
                    },
                    "PERCEIVE": {
                        "sensor_results": [
                            {"name": sr.name, "outcome": jsonable(sr.outcome), "mass": _mass_doc(sr.output.mass),
                             "features": jsonable(dict(sr.output.features)),
                             "vacuous_reason": sr.output.vacuous_reason, "error": sr.error_text}
                            for sr in info.results
                        ],
                        "stage1_assessment": _assessment_doc(info.stage1),
                    },
                    "REMEMBER": {
                        # Behavioural_DNA comparison: each sensor's features against the
                        # account's profile before this event.
                        "dna_comparison": {sr.name: jsonable(dict(sr.output.features))
                                           for sr in info.results if sr.output.features},
                        "change_point": None if info.cp is None else {
                            "kind": jsonable(info.cp.outcome), "short_run_mass": float(info.cp.short_run_mass),
                            "est_start": info.cp.est_start.isoformat()},
                    },
                    "REASON": {
                        "motifs": _motif_hits(info.stage2),
                        "truncated_motif_types": sorted(jsonable(m) for m in info.truncated),
                        "stage2_assessment": _assessment_doc(info.stage2),
                    },
                    "ACT": act,
                    "LEARN": learn,
                },
            }
        stored = state.storage.assessments(tx_id) if state.storage is not None else []
        if not stored:
            raise ApiError(404, f"unknown Transaction_Event {tx_id!r}: not held by the pipeline or in Storage",
                           tx_id=tx_id)
        by_stage = {sa.assessment.stage: sa for sa in stored}
        s1, s2 = by_stage.get(1), by_stage.get(2)
        last = s2 or s1
        assert last is not None
        act, learn = _act_learn(tx_id, last.assessment, last.risk)
        return {
            "tx_id": tx_id,
            "timestamp": None,
            "source": "storage",
            "event": None,
            "stages": {
                "SENSE": {},
                "PERCEIVE": {
                    "sensor_results": [
                        {"name": c.contributor_id, "outcome": "SUCCESS", "mass": _mass_doc(c.mass),
                         "features": {}, "vacuous_reason": None, "error": None}
                        for c in (s1.assessment.contributions if s1 else ()) if c.kind == "SENSOR"
                    ],
                    "stage1_assessment": _assessment_doc(s1.assessment) if s1 else None,
                },
                "REMEMBER": {"dna_comparison": {}, "change_point": None},
                "REASON": {
                    "motifs": _motif_hits(s2.assessment) if s2 else [],
                    "truncated_motif_types": [],
                    "stage2_assessment": _assessment_doc(s2.assessment) if s2 else None,
                },
                "ACT": act,
                "LEARN": learn,
            },
        }

    return r


# --- WebSockets --------------------------------------------------------------------------------


async def _pump(ws: WebSocket, client: Client) -> None:
    while True:
        msg = await client.queue.get()
        await ws.send_text(dumps(msg))
        client.sent += 1


async def _until_disconnect(ws: WebSocket) -> None:
    while True:
        m = await ws.receive()
        if m.get("type") == "websocket.disconnect":
            return


async def serve_channel(ws: WebSocket, state: AppState, channel: str, guard: Optional[WSGuard]) -> None:
    """One subscriber: bounded queue (oldest dropped when full), served until disconnect.
    The pipeline never waits for the client (Req 1.13)."""
    if guard is not None and not await guard(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    client = state.hub.register(channel)
    tasks = [asyncio.ensure_future(_pump(ws, client)), asyncio.ensure_future(_until_disconnect(ws))]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, (WebSocketDisconnect, RuntimeError, OSError)):
                raise exc
    finally:
        for t in tasks:
            t.cancel()
        state.hub.unregister(client)
        if ws.application_state is WebSocketState.CONNECTED and ws.client_state is WebSocketState.CONNECTED:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 - already gone
                pass


def _install_websockets(app: FastAPI, state: AppState, guard: Optional[WSGuard]) -> None:
    def make(channel: str) -> Callable[[WebSocket], Awaitable[None]]:
        async def endpoint(ws: WebSocket) -> None:
            await serve_channel(ws, state, channel, guard)

        endpoint.__name__ = f"ws_{channel}"
        return endpoint

    for ch in WS_CHANNELS:
        app.add_api_websocket_route(f"/ws/{ch}", make(ch), name=f"ws_{ch}")


__all__ = ["API_PREFIX", "ApiError", "WSGuard", "check_runtime_patch", "create_app", "mount_once", "secure_app",
           "serve_channel"]
