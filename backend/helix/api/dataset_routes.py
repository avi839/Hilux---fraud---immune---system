"""Dataset streaming routes (Req 39.5, 39.6, 39.8; design §24) under ``/api/v1``.

Mounted by ``create_app`` (``include_dataset_routes``) on every app, after the core router.
No authentication (prototype scope, Req 27): serve on loopback only.

- ``GET /dataset``: the Dataset_Feeder status and progress, the Warm_Up_Mode report and the
  Evaluation_Monitor's dataset summary with per-AMLworld-pattern-type metrics. Without
  ``--dataset`` the status is ``DISABLED`` and every other field is null.
- ``POST /sim/generator/start`` is guarded: while dataset streaming is active it answers 409
  ``{"detail": "dataset streaming is active"}``; otherwise the core handler runs unchanged.
  The guard is a route placed in front of the core one (inside the middleware stack, so the
  security and CORS middleware still apply).

Ground truth is read only through ``EvaluationMonitor.dataset_summary()``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from helix.datasets.feed import FeedStatus

from .app import API_PREFIX, ApiError, mount_once
from .serialize import jsonable

GENERATOR_START = f"{API_PREFIX}/sim/generator/start"
DATASET_ACTIVE = "dataset streaming is active"
_NULL_FIELDS = ("kind", "files", "rows_read", "skipped", "skip_reasons", "in_scope", "submitted", "progress",
                "speed", "max_rows", "warmup", "error", "evaluation")


def _ratio(v: Any) -> Optional[float]:
    return None if v is None or isinstance(v, str) else float(v)


def evaluation_doc(monitor: Any) -> Optional[dict[str, Any]]:
    """Dataset summary against the loader's labels (Req 4.20, 4.21, 39.6)."""
    if monitor is None:
        return None
    s = monitor.dataset_summary()
    types = []
    for t, st in s.pattern_types.items():
        if not st.mapped:
            why: Optional[str] = "unmapped"
        elif isinstance(st.precision, str):
            why = "no_cases"  # no admitted Case holds the mapped Motif type yet
        else:
            why = None
        types.append({
            "pattern_type": t, "mapped": st.mapped, "motif": st.motif, "patterns": st.patterns,
            "detected_patterns": st.detected_patterns, "detection_rate": _ratio(st.detection_rate),
            "recall": _ratio(st.recall), "precision": _ratio(st.precision), "precision_reason": why,
        })
    return {
        "precision": _ratio(s.precision), "recall": _ratio(s.recall),
        "captured_fraud_fraction": _ratio(s.captured_fraud_fraction), "pattern_types": types,
    }


def _warmup_doc(feeder: Any) -> Optional[dict[str, Any]]:
    spec = feeder.spec
    if not spec.warmup:
        return None
    r = feeder.warmup_report
    doc: dict[str, Any] = {"until": spec.warmup_until, "done": r is not None, "events": None, "duration_s": None,
                           "events_per_s": None, "accounts": None, "accounts_ready": None, "min_history": None}
    if r is not None:
        doc.update(events=r.events, duration_s=r.duration_s, events_per_s=r.events_per_s, accounts=r.accounts,
                   accounts_ready=r.accounts_ready, min_history=r.min_history)
    return doc


def dataset_doc(state: Any) -> dict[str, Any]:
    feeder = getattr(state, "dataset", None)
    if feeder is None:
        return {"status": FeedStatus.DISABLED.value, **{k: None for k in _NULL_FIELDS}}
    p = feeder.progress()
    spec = feeder.spec
    return jsonable({
        "status": p.status.value,
        "kind": spec.kind,
        "files": {"trans": str(spec.trans), "patterns": None if spec.patterns is None else str(spec.patterns),
                  "mapping": None if spec.mapping is None else str(spec.mapping)},
        "rows_read": p.rows_read,
        "skipped": p.skipped,
        "skip_reasons": dict(sorted(p.skip_reasons.items())),
        "in_scope": p.in_scope,
        "submitted": p.submitted,
        "progress": p.fraction,
        "speed": spec.speed,
        "max_rows": spec.max_rows,
        "warmup": _warmup_doc(feeder),
        "error": p.error,
        "evaluation": evaluation_doc(getattr(state, "monitor", None)),
    })


def dataset_router(state: Any) -> APIRouter:
    r = APIRouter(prefix=API_PREFIX)

    @r.get("/dataset", tags=["dataset"])
    async def get_dataset() -> Any:
        """Dataset_Feeder status, progress, warm-up report and per-pattern evaluation."""
        return dataset_doc(state)

    return r


def _guard_generator_start(app: FastAPI, state: Any) -> None:
    routes = app.router.routes
    idx = next((i for i, rt in enumerate(routes)
                if isinstance(rt, APIRoute) and rt.path == GENERATOR_START and "POST" in rt.methods), None)
    if idx is None:  # pragma: no cover - the core router always has it
        return
    core = routes[idx]

    async def guarded_start_generator() -> Any:
        if getattr(state, "dataset", None) is not None:
            raise ApiError(409, DATASET_ACTIVE)
        return await core.endpoint()

    guard = APIRoute(GENERATOR_START, guarded_start_generator, methods=["POST"], response_model=core.response_model,
                     include_in_schema=False, name="guarded_start_generator")
    routes.insert(idx, guard)


def include_dataset_routes(app: FastAPI, state: Any) -> FastAPI:
    """Mount ``GET /dataset`` and the generator-start guard (idempotent)."""
    if "dataset" in (getattr(app.state, "mounted_routers", None) or set()):
        return app
    _guard_generator_start(app, state)
    return mount_once(app, "dataset", lambda: dataset_router(state))


__all__ = ["dataset_doc", "dataset_router", "evaluation_doc", "include_dataset_routes"]
