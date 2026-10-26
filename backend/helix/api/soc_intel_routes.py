"""Threat-intel and Audit_Ledger REST routes (Req 34, 35.4, 35.7), mounted with
``include_intel_routes``.

Routes (prefix ``/api/v1``):

- ``GET  /soc/indicators`` (``?type=ACCOUNT&include_expired=true&offset=0&limit=100``):
  ``{count, total, offset, limit, indicators: [{value, type, confidence, source, first_seen,
  last_seen, expiry, tlp, expired}], summary}``.
- ``GET  /soc/indicators/export`` (``?include_red=false``): a STIX 2.1-style bundle; TLP:RED
  Indicators only with ``include_red=true`` (Req 34.8).
- ``POST /soc/indicators/import``: body is a STIX bundle (JSON, at most 5 MiB). 200 ``{imported,
  rejected: [{id, errors: [{field, reason}]}], skipped, indicators}``: valid objects are stored
  and each invalid one is reported (Req 34.6, 34.7); 422 when the body is not a bundle, 413
  when too large.
- ``GET  /soc/audit`` (``?offset=0&limit=100&category=PLAYBOOK_ACTION``): ``{total, offset, limit,
  head, entries: [{index, ts, category, actor, payload, prev_hash, hash}]}``.
- ``GET  /soc/audit/verify``: ``{valid, first_broken_index, head, entries, reason, notice:
  "tamper-evident, not tamper-proof"}`` (Req 35.4, 35.7, 35.8).

Design decisions:
- The Indicator_Store and Audit_Ledger are taken from ``state.indicators`` / ``state.ledger``
  (or ``state.helix.indicators`` / ``.ledger``). When neither exists the router creates one
  (Indicator_Store on the pipeline's stream clock, Audit_Ledger on the wall clock, both
  in-memory) and stores it on ``state`` so the integration step can reach it. Wiring them into
  ``create_app`` and the pipeline is a later integration step.
- Same conventions as ``helix.api.soc_routes``: ``ApiError`` -> ``{"detail", ...}``, values
  through ``jsonable``.
- No authentication or authorisation, like the rest of the API (prototype scope, Req 27):
  serve on loopback only. Import changes detection behaviour and the audit view exposes
  investigator names, so a deployment beyond loopback needs an authenticated role here.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, Query, Request

from helix.soc.intel import BundleError, IndicatorStore, IndicatorType, attach_indicators
from helix.soc.ledger import MAX_PAGE, NOTICE, AuditLedger, LedgerError, attach_audit

from .app import API_PREFIX, ApiError
from .serialize import jsonable

INTEL_TAG = "soc-intel"
AUDIT_TAG = "soc-audit"
MAX_IMPORT_BYTES = 5 * 1024 * 1024
E413 = {413: {"description": "bundle larger than 5 MiB"}}
E422 = {422: {"description": "not a STIX bundle or invalid query; nothing was imported"}}


def _first(*objs: Any) -> Any:
    """First object that is not None (the stores define ``__len__``, so ``or`` is wrong)."""
    return next((o for o in objs if o is not None), None)


def resolve_indicators(state: Any) -> IndicatorStore:
    store = _first(getattr(state, "indicators", None), getattr(state.helix, "indicators", None))
    if store is None:
        store = IndicatorStore()
        attach_indicators(state.helix, store)
    if getattr(state, "indicators", None) is None:
        state.indicators = store
    return store


def resolve_ledger(state: Any) -> AuditLedger:
    ledger = _first(getattr(state, "ledger", None), getattr(state.helix, "ledger", None))
    if ledger is None:
        ledger = AuditLedger()
        attach_audit(state.helix, ledger)
    if getattr(state, "ledger", None) is None:
        state.ledger = ledger
    return ledger


def intel_router(state: Any) -> APIRouter:
    r = APIRouter(prefix=API_PREFIX)
    store = resolve_indicators(state)
    ledger = resolve_ledger(state)

    import_body = {"requestBody": {"required": True, "description": "a STIX 2.1-style bundle",
                                   "content": {"application/json": {"schema": {"type": "object"}}}}}

    @r.get("/soc/indicators", tags=[INTEL_TAG], responses=E422)
    async def list_indicators(
        type: Optional[IndicatorType] = Query(None, description="ACCOUNT, DEVICE, IP or MERCHANT"),  # noqa: A002
        include_expired: bool = Query(True),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=MAX_PAGE),
    ) -> Any:
        """Stored Indicators with their expiry state at the current stream time (Req 34.1)."""
        now = store.clock()
        rows = [i for i in store.all() if (type is None or i.type is type)
                and (include_expired or not i.expired_at(now))]
        page = rows[offset:offset + limit]
        return jsonable({"count": len(page), "total": len(rows), "offset": offset, "limit": limit,
                         "indicators": [{**i.to_dict(), "expired": i.expired_at(now)} for i in page],
                         "summary": store.summary(now)})

    @r.get("/soc/indicators/export", tags=[INTEL_TAG])
    async def export_indicators(include_red: bool = Query(False, description="include TLP:RED Indicators")) -> Any:
        """STIX 2.1-style bundle; TLP:RED only when explicitly requested (Req 34.5, 34.8)."""
        return store.export_bundle(include_red=include_red)

    @r.post("/soc/indicators/import", tags=[INTEL_TAG], openapi_extra=import_body, responses={**E413, **E422})
    async def import_indicators(request: Request) -> Any:
        """Import a STIX bundle: valid Indicator objects are stored, invalid ones reported (Req 34.6, 34.7)."""
        raw = await request.body()
        if len(raw) > MAX_IMPORT_BYTES:
            raise ApiError(413, f"bundle larger than {MAX_IMPORT_BYTES} bytes")
        try:
            doc = json.loads(raw)
        except ValueError:
            raise ApiError(422, "body must be a JSON STIX bundle") from None
        try:
            rep = store.import_bundle(doc)
        except BundleError as e:
            raise ApiError(422, str(e)) from None
        return jsonable(rep.to_dict())

    @r.get("/soc/audit", tags=[AUDIT_TAG], responses=E422)
    async def list_audit(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=MAX_PAGE),
                         category: Optional[str] = Query(None, max_length=64)) -> Any:
        """Audit_Ledger entries in index order, paged (Req 35.1-35.3)."""
        try:
            total, rows = ledger.page(offset, limit, category)
        except LedgerError as e:
            raise ApiError(422, str(e)) from None
        return jsonable({"total": total, "offset": offset, "limit": limit, "head": ledger.head().to_dict(),
                         "entries": [e.to_dict() for e in rows]})

    @r.get("/soc/audit/verify", tags=[AUDIT_TAG])
    async def verify_audit() -> Any:
        """Recompute the hash chain against the head record (Req 35.4, 35.7, 35.8)."""
        res = ledger.verify()
        return jsonable({**res.to_dict(), "notice": NOTICE})

    return r


def include_intel_routes(app: FastAPI, state: Any) -> FastAPI:
    """Mount the threat-intel and audit routes on ``app`` (``create_app`` already does; idempotent)."""
    from .app import mount_once

    return mount_once(app, "soc_intel", lambda: intel_router(state))


__all__ = ["include_intel_routes", "intel_router", "resolve_indicators", "resolve_ledger"]
