"""SOC / cyber-fusion REST routes (Req 28.1-28.3, 29.4-29.6, 30, 31), mounted with ``include_soc_routes``.

Design decisions:
- Kept out of ``app.py``: ``include_soc_routes(app, state)`` adds one router to an app built by
  ``create_app(state)``, so the routes reuse its error handlers (``ApiError`` → ``{"detail",
  ...}``, ``CaseNotFound`` → 404). Wiring into ``create_app`` / ``python -m helix.api`` is a
  later integration step.
- ``POST /api/v1/auth-events`` takes one Auth_Event JSON document and hands the raw body to
  ``StreamIngestor.submit_auth``, which validates it (undefined keys ignored), routes rejects
  to the rejected-events log and enqueues accepted events on the account's queue. Responses
  mirror ``POST /events``: 202 ``{event_id, status, duplicate, backpressure}`` with an
  ``X-Backpressure`` header, 422 with ``[{field, reason, rule}]``. With ``soc.enabled`` off
  the route answers 503 and admits nothing, because the pipeline would discard the event.
- ``GET /api/v1/cases/{case_id}/killchain`` reconstructs the kill chain from the Case's
  current Evidence_Chain (``Helix.kill_chain``): stages in stage order with the earliest entry
  timestamp, the most advanced stage, per-entry assignments with unmapped marks, and the
  Kill_Chain_Multiplier as applied to Expected_Loss. It works with the flag off too (read
  only); ``multiplier.enabled`` then reports False.
- Technique mapping (Req 30): ``GET /api/v1/soc/techniques`` (catalogue with version and the
  "not official MITRE data" notice), ``GET /api/v1/soc/coverage`` (Coverage_Matrix of the running
  pipeline) and ``GET /api/v1/cases/{case_id}/techniques`` (the Case's technique tags derived
  from its Evidence_Chain). Read-only; they work with the flag off.
- Detection-as-code (Req 31): ``POST /api/v1/soc/rules`` takes the YAML either as the raw body
  (any non-JSON content type) or as JSON ``{"yaml": "..."}``; 201 with the stored version and
  its backtest, 422 ``{"detail", "errors": [{line, field, reason}]}`` with nothing changed.
  ``GET /api/v1/soc/rules``, ``GET /api/v1/soc/rules/{rule_id}``, ``POST
  .../{rule_id}/versions/{version}/backtest`` (optional JSON ``{"window_s": n}``), ``POST
  .../{rule_id}/activate`` (optional JSON ``{"version": n, "by": "..."}``; 409 when not
  backtested) and ``POST .../{rule_id}/deactivate``. The engine is ``helix.rules`` when one is
  attached; otherwise one is created for the router and attached when ``soc.enabled`` is on.
  With the flag off rules can be authored and backtested, but activation answers 503 because the
  pipeline does not evaluate rules then.
- No authentication or authorisation, like the rest of the API (prototype scope, Req 27):
  serve on loopback only. Rule activation changes detection behaviour, so a deployment beyond
  loopback needs an authenticated investigator role on these routes.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, Path, Request, Response
from fastapi.responses import JSONResponse

from helix.cases import MULTIPLIED_STAGES
from helix.codec import AuthEvent
from helix.ingest import IngestStatus
from helix.soc.coverage import pipeline_coverage
from helix.soc.rules import (
    MAX_RULE_BYTES,
    RuleEngine,
    RuleNotFound,
    RuleStateError,
    RuleValidationError,
    attach_rules,
    rule_engine_for,
)
from helix.soc.techniques import chain_techniques, default_catalogue, default_mapping

from .app import API_PREFIX, ApiError, problem
from .serialize import jsonable

SOC_TAG = "soc"
E404 = {404: {"description": "unknown Case"}}
E404R = {404: {"description": "unknown Detection_Rule or version"}}
E409 = {409: {"description": "the version has not been backtested"}}
E422 = {422: {"description": "validation failed; nothing was changed"}}
E503 = {503: {"description": "cyber fusion (soc.enabled) is off; nothing was admitted"}}
RULE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


def kill_chain_doc(helix: Any, case_id: str) -> dict[str, Any]:
    """JSON document for one Case's kill chain (Req 29.4-29.6)."""
    case = helix.cases.get(case_id)  # CaseNotFound -> 404
    kc = helix.kill_chain(case_id)
    soc = helix.cfg.soc
    enabled = bool(getattr(helix, "soc_enabled", False))
    most = None if kc is None else kc.most_advanced
    doc: dict[str, Any] = {
        "case_id": case_id,
        "most_advanced": None if most is None else most.value,
        "stages": [] if kc is None else [s.to_dict() for s in kc.stages],
        "entries": [] if kc is None else [
            {"contributor_id": c, "tx_id": t, "stage": a.stage.value, "key": a.key, "mapped": a.mapped}
            for (c, t), a in kc.assignments
        ],
        "unmapped": [] if kc is None else [{"contributor_id": c, "tx_id": t} for c, t in kc.unmapped],
        "default_stage": soc.kill_chain_default_stage.value,
        "multiplier": {
            "enabled": enabled,
            "value": float(soc.kill_chain_multiplier),
            # the stage the Prioritiser used (a closed Case keeps its last open-time stage)
            "applied": bool(enabled and case.kill_chain_stage in MULTIPLIED_STAGES),
            "expected_loss": str(case.expected_loss),
            "expected_loss_base": None if case.expected_loss_base is None else str(case.expected_loss_base),
            "priority": str(case.priority),
        },
    }
    return jsonable(doc)


def techniques_doc() -> dict[str, Any]:
    """HELIX_Technique_Catalogue payload (Req 30.1, 30.8)."""
    return default_catalogue().to_dict()


def case_techniques_doc(helix: Any, case_id: str) -> dict[str, Any]:
    """A Case's HELIX_Technique tags: the union over its Evidence_Chain contributors (Req 30.4)."""
    helix.cases.get(case_id)  # CaseNotFound -> 404
    cat = default_catalogue()
    entries = chain_techniques(helix.chain(case_id) or (), cat, default_mapping())
    tags = sorted({t for e in entries for t in e.techniques})
    return {"case_id": case_id, "catalogue_version": cat.version, "notice": cat.notice,
            "techniques": [cat.get(t).to_dict() for t in tags],  # type: ignore[union-attr]
            "contributors": [e.to_dict() for e in entries]}


def _engine(state: Any) -> RuleEngine:
    helix = state.helix
    eng = getattr(helix, "rules", None)
    if eng is None:
        eng = rule_engine_for(helix, getattr(state, "storage", None))
        if getattr(helix, "soc_enabled", False):
            attach_rules(helix, eng, storage=getattr(state, "storage", None))
    return eng


def _rule_errors(e: RuleValidationError) -> JSONResponse:
    errs = [{"line": p.line, **problem(p.path or "document", p.message)} for p in e.problems]
    return JSONResponse({"detail": "invalid Detection_Rule", "errors": errs}, status_code=422)


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


def soc_router(state: Any) -> APIRouter:
    r = APIRouter(prefix=API_PREFIX)
    helix = state.helix
    CaseId = Path(min_length=1, max_length=64, description="Case identifier, e.g. C-12")
    RuleId = Path(pattern=RULE_ID_PATTERN, description="Detection_Rule identifier")
    engine = _engine(state)

    auth_body = {
        "requestBody": {
            "required": True,
            "description": "one Auth_Event JSON document; undefined keys are ignored",
            "content": {"application/json": {"schema": AuthEvent.model_json_schema()}},
        }
    }
    rule_body = {
        "requestBody": {
            "required": True,
            "description": "one Detection_Rule YAML document, raw or as JSON {\"yaml\": \"...\"}",
            "content": {
                "application/yaml": {"schema": {"type": "string"}},
                "application/json": {"schema": {"type": "object", "required": ["yaml"],
                                                "properties": {"yaml": {"type": "string"}}}},
            },
        }
    }

    @r.post("/auth-events", status_code=202, tags=[SOC_TAG], openapi_extra=auth_body,
            responses={**E422, **E503, 202: {"description": "accepted or duplicate; X-Backpressure header set"}})
    async def ingest_auth_event(request: Request, response: Response) -> Any:
        """Ingest one Auth_Event (Req 28.1-28.3). Duplicates are acknowledged, not re-admitted."""
        if not getattr(helix, "soc_enabled", False):
            raise ApiError(503, "cyber fusion is disabled (soc.enabled is false); Auth_Event not admitted")
        res = state.ingestor.submit_auth(await request.body())
        bp = "true" if res.backpressure else "false"
        if res.status is IngestStatus.REJECTED:
            errs = [problem(e.field, e.message, e.rule) for e in res.error.errors]  # type: ignore[union-attr]
            return JSONResponse({"detail": "invalid Auth_Event", "errors": errs}, status_code=422,
                                headers={"X-Backpressure": bp})
        response.headers["X-Backpressure"] = bp
        return {"event_id": res.tx_id or "", "status": res.status.value,
                "duplicate": res.status is IngestStatus.DUPLICATE, "backpressure": res.backpressure}

    @r.get("/cases/{case_id}/killchain", tags=[SOC_TAG], responses={**E404, **E422})
    async def get_kill_chain(case_id: str = CaseId) -> Any:
        """Kill_Chain_Stages of a Case in stage order and its most advanced stage (Req 29.4, 29.5)."""
        return kill_chain_doc(helix, case_id)

    # --- Req 30: techniques and coverage ----------------------------------------------

    @r.get("/soc/techniques", tags=[SOC_TAG])
    async def get_techniques() -> Any:
        """HELIX_Technique_Catalogue with its version and the not-official-MITRE notice (Req 30.1, 30.8)."""
        return techniques_doc()

    @r.get("/soc/coverage", tags=[SOC_TAG])
    async def get_coverage() -> Any:
        """Coverage_Matrix: detecting contributors, attacker detection rate and gaps (Req 30.6-30.8)."""
        cm = pipeline_coverage(helix, rules=engine, attacker=getattr(state, "attacker", None))
        return jsonable(cm.to_dict())

    @r.get("/cases/{case_id}/techniques", tags=[SOC_TAG], responses={**E404, **E422})
    async def get_case_techniques(case_id: str = CaseId) -> Any:
        """HELIX_Technique tags of a Case and the contributors behind them (Req 30.4)."""
        return jsonable(case_techniques_doc(helix, case_id))

    # --- Req 31: detection rules ---------------------------------------------------------

    @r.get("/soc/rules", tags=[SOC_TAG])
    async def list_rules() -> Any:
        """Every Detection_Rule with its latest and active version and the version history."""
        return jsonable({"rules": engine.summary(), "eval_errors": engine.eval_errors,
                         "backtest_window_s": engine.backtest_window_s})

    @r.post("/soc/rules", status_code=201, tags=[SOC_TAG], openapi_extra=rule_body,
            responses={**E422, 201: {"description": "stored as a new version and backtested"}})
    async def submit_rule(request: Request) -> Any:
        """Validate and store a Detection_Rule as a new version, then backtest it (Req 31.1-31.4)."""
        raw = await request.body()
        if len(raw) > MAX_RULE_BYTES:
            raise ApiError(413, f"Detection_Rule document larger than {MAX_RULE_BYTES} bytes")
        text: Any = raw
        if "json" in (request.headers.get("content-type") or "").lower():
            doc = await _json_body(request)
            if not isinstance(doc.get("yaml"), str):
                raise ApiError(422, "JSON body needs a string field 'yaml'")
            text = doc["yaml"]
        try:
            rv = engine.submit(text, source="api")
        except RuleValidationError as e:
            return _rule_errors(e)
        return jsonable({**rv.to_dict(), "active_version": engine.active_version(rv.rule.rule_id)})

    @r.get("/soc/rules/{rule_id}", tags=[SOC_TAG], responses={**E404R, **E422})
    async def get_rule(rule_id: str = RuleId) -> Any:
        """All stored versions of one Detection_Rule (YAML and backtest per version)."""
        try:
            vs = engine.versions(rule_id)
        except RuleNotFound:
            raise ApiError(404, f"unknown Detection_Rule {rule_id!r}") from None
        return jsonable({"rule_id": rule_id, "active_version": engine.active_version(rule_id),
                         "latest_version": vs[-1].version, "versions": [v.to_dict() for v in vs]})

    @r.post("/soc/rules/{rule_id}/versions/{version}/backtest", tags=[SOC_TAG],
            responses={**E404R, **E422})
    async def backtest_rule_version(request: Request, rule_id: str = RuleId,
                                    version: int = Path(ge=1, le=1_000_000)) -> Any:
        """Backtest one version over the stored events of the window (default 7 days, Req 31.4)."""
        doc = await _json_body(request)
        window = doc.get("window_s")
        if window is not None and (isinstance(window, bool) or not isinstance(window, (int, float))
                                   or not 60 <= window <= 90 * 86_400):
            raise ApiError(422, "window_s must be a number of seconds in [60, 7776000]")
        try:
            rep = engine.backtest(rule_id, version, window_s=window)
        except RuleNotFound:
            raise ApiError(404, f"unknown Detection_Rule {rule_id!r} version {version}") from None
        except RuleStateError as e:
            raise ApiError(409, str(e)) from None
        return jsonable(rep.to_dict())

    @r.post("/soc/rules/{rule_id}/activate", tags=[SOC_TAG], responses={**E404R, **E409, **E422, **E503})
    async def activate_rule(request: Request, rule_id: str = RuleId) -> Any:
        """Activate a backtested version (default latest); applies to the next event (Req 31.5)."""
        doc = await _json_body(request)
        version: Optional[int] = doc.get("version")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
            raise ApiError(422, "version must be an integer >= 1")
        by = doc.get("by", "investigator")
        if not isinstance(by, str) or not 1 <= len(by) <= 64:
            raise ApiError(422, "by must be a string of 1-64 characters")
        if not getattr(helix, "soc_enabled", False):
            raise ApiError(503, "cyber fusion is disabled (soc.enabled is false); rules are not evaluated")
        try:
            rv = engine.activate(rule_id, version, by=by)
        except RuleNotFound:
            raise ApiError(404, f"unknown Detection_Rule {rule_id!r}" + ("" if version is None else f" version {version}")) from None
        except RuleStateError as e:
            raise ApiError(409, str(e)) from None
        return jsonable({"rule_id": rule_id, "active_version": rv.version,
                         "activated_at": rv.activated_at, "activated_by": rv.activated_by})

    @r.post("/soc/rules/{rule_id}/deactivate", tags=[SOC_TAG], responses={**E404R, **E422})
    async def deactivate_rule(rule_id: str = RuleId) -> Any:
        """Stop evaluating a Detection_Rule (its versions are kept)."""
        try:
            old = engine.deactivate(rule_id)
        except RuleNotFound:
            raise ApiError(404, f"unknown Detection_Rule {rule_id!r}") from None
        return {"rule_id": rule_id, "active_version": None, "previous_active_version": old}

    return r


def include_soc_routes(app: FastAPI, state: Any) -> FastAPI:
    """Mount the SOC routes on ``app`` (``create_app`` already does; idempotent). Returns ``app``."""
    from .app import mount_once

    return mount_once(app, "soc", lambda: soc_router(state))


__all__ = ["case_techniques_doc", "include_soc_routes", "kill_chain_doc", "soc_router", "techniques_doc"]
