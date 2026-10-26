"""API_Server security controls for the local prototype (design Â§21, Req 27.1-27.4).

HELIX implements no authentication or authorisation. These controls narrow who can reach
it from a browser on the same machine; they are not a substitute for auth:

- bind check: a non-loopback bind address logs a warning naming the address and the
  missing auth before serving (Req 27.2);
- CORS: only the configured Dashboard origin is allowed, echoed exactly, never ``*``
  (Req 27.3);
- WebSocket origin guard: a handshake whose ``Origin`` is missing or differs from the
  Dashboard origin is closed with 1008 before ``accept()``, so no data is sent (Req 27.4);
- security headers on HTTP responses (``nosniff``, ``no-referrer``, ``no-store`` on /api);
- request body size limit (default 1 MiB) on POST/PUT/PATCH, answered with 413.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket

if TYPE_CHECKING:  # pragma: no cover
    from .state import AppState

log = logging.getLogger("helix.api.security")

DEFAULT_MAX_BODY_BYTES = 1024 * 1024  # 1 MiB
CORS_METHODS = ("GET", "POST", "PATCH")
CORS_HEADERS = ("content-type",)
LIMITED_METHODS = frozenset({"POST", "PUT", "PATCH"})
LOOPBACK_NAMES = frozenset({"localhost"})

WSGuard = Callable[[WebSocket], Awaitable[bool]]


# --- bind address ------------------------------------------------------------------------


def is_loopback(host: str) -> bool:
    """True for addresses in 127.0.0.0/8, ``::1`` and the name ``localhost``.

    ``0.0.0.0``, ``::``, every other IP and every other hostname (no DNS lookup is made)
    are non-loopback. A bracketed IPv6 literal (``[::1]``) is accepted."""
    h = host.strip()
    if h.lower().rstrip(".") in LOOPBACK_NAMES:
        return True
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def bind_warning(host: str) -> str:
    return (f"HELIX API_Server is binding to non-loopback address {host!r}: no authentication or "
            "authorisation is implemented, so anyone who can reach this address can read and change "
            "HELIX state. Bind to 127.0.0.1 unless the network is trusted (Req 27.2).")


def check_bind(host: str, logger: Optional[logging.Logger] = None) -> bool:
    """Call before serving. Returns True for a loopback bind; otherwise logs a WARNING that
    names the address and states that no authentication/authorisation exists, and returns
    False (serving still proceeds: the warning is the control, Req 27.2)."""
    if is_loopback(host):
        return True
    (logger or log).warning(bind_warning(host))
    return False


# --- CORS ----------------------------------------------------------------------------------


def validate_origin(origin: str) -> str:
    """The Dashboard origin must be a single ``scheme://host[:port]`` (http/https), never a
    wildcard or a list, so the CORS response can only echo that exact origin."""
    if not isinstance(origin, str) or not origin or origin != origin.strip():
        raise ValueError(f"invalid dashboard origin {origin!r}")
    if "*" in origin or "," in origin or any(c.isspace() for c in origin):
        raise ValueError(f"dashboard origin must be one exact origin, not {origin!r}")
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path or parts.query or parts.fragment:
        raise ValueError(f"dashboard origin must be scheme://host[:port], not {origin!r}")
    return origin


def cors_middleware(origin: str) -> Middleware:
    """CORS allowing exactly ``origin``; no credentials, no regex, no wildcard."""
    return Middleware(
        CORSMiddleware,
        allow_origins=[validate_origin(origin)],
        allow_origin_regex=None,
        allow_methods=list(CORS_METHODS),
        allow_headers=list(CORS_HEADERS),
        allow_credentials=False,
        expose_headers=["X-Backpressure"],
        max_age=600,
    )


# --- WebSocket origin -------------------------------------------------------------------------


def make_ws_origin_guard(state: AppState, origin: str) -> WSGuard:
    """Handshake guard (run before ``accept()``): allow only a single ``Origin`` header
    equal to ``origin``. A missing header is allowed only when
    ``state.allow_missing_ws_origin`` is set (tests, non-browser clients). Every rejection
    increments ``state.ws_origin_rejected``."""
    origin = validate_origin(origin)

    async def guard(ws: WebSocket) -> bool:
        got = ws.headers.getlist("origin")
        ok = (len(got) == 1 and got[0] == origin) or (not got and state.allow_missing_ws_origin)
        if not ok:
            state.ws_origin_rejected += 1
            shown = "missing" if not got else repr(got[0][:200])  # repr: no log injection
            log.warning("rejected WebSocket %s: origin %s is not the dashboard origin",
                        ws.url.path, shown)
        return ok

    return guard


# --- HTTP middleware -------------------------------------------------------------------------


class SecurityHeadersMiddleware:
    """Adds ``X-Content-Type-Options: nosniff`` and ``Referrer-Policy: no-referrer`` to every
    HTTP response and ``Cache-Control: no-store`` to every ``/api`` response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        api = scope.get("path", "").startswith("/api")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h["X-Content-Type-Options"] = "nosniff"
                h["Referrer-Policy"] = "no-referrer"
                if api:
                    h["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyTooLarge(HTTPException):
    """Raised from ``receive`` when a body without (or with a lying) Content-Length passes the
    limit. An HTTPException so FastAPI re-raises it unchanged and renders 413."""

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"request body exceeds {limit} bytes")


class BodyLimitMiddleware:
    """413 for POST/PUT/PATCH bodies larger than ``max_bytes``: immediately when the
    Content-Length says so (the body is not read), otherwise as soon as the streamed body
    passes the limit. ``on_reject`` is called once per rejected request."""

    def __init__(self, app: ASGIApp, max_bytes: int = DEFAULT_MAX_BODY_BYTES,
                 on_reject: Optional[Callable[[], None]] = None) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        self.app, self.max_bytes, self.on_reject = app, int(max_bytes), on_reject

    def _rejected(self) -> None:
        if self.on_reject is not None:
            self.on_reject()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in LIMITED_METHODS:
            await self.app(scope, receive, send)
            return
        limit = self.max_bytes
        declared = [v for k, v in scope.get("headers", ()) if k == b"content-length"]
        if declared:
            try:
                n = int(declared[0])
            except ValueError:
                n = -1
            if n < 0 or len(declared) > 1:
                await JSONResponse({"detail": "invalid Content-Length"}, status_code=400)(scope, receive, send)
                return
            if n > limit:
                self._rejected()
                await JSONResponse({"detail": f"request body exceeds {limit} bytes"},
                                   status_code=413)(scope, receive, send)
                return
        seen = 0

        async def limited() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    self._rejected()
                    raise BodyTooLarge(limit)
            return msg

        await self.app(scope, limited, send)


# --- assembly ------------------------------------------------------------------------------


def security_middleware(state: AppState, origin: str, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> list[Middleware]:
    """Outermost first: security headers (so CORS preflights get them too), CORS, body limit."""

    def count() -> None:
        state.body_too_large += 1

    return [
        Middleware(SecurityHeadersMiddleware),
        cors_middleware(origin),
        Middleware(BodyLimitMiddleware, max_bytes=max_bytes, on_reject=count),
    ]


def chain_guards(*guards: Optional[WSGuard]) -> Optional[WSGuard]:
    gs = [g for g in guards if g is not None]
    if not gs:
        return None
    if len(gs) == 1:
        return gs[0]

    async def guard(ws: WebSocket) -> bool:
        for g in gs:
            if not await g(ws):
                return False
        return True

    return guard


def security_stats(state: Any) -> dict[str, Any]:
    return {
        "authentication": False,  # prototype: none implemented (Req 27)
        "dashboard_origin": state.cfg.api.dashboard_origin,
        "allow_missing_ws_origin": state.allow_missing_ws_origin,
        "ws_origin_rejected": state.ws_origin_rejected,
        "body_too_large": state.body_too_large,
    }


__all__ = [
    "CORS_HEADERS",
    "CORS_METHODS",
    "DEFAULT_MAX_BODY_BYTES",
    "BodyLimitMiddleware",
    "BodyTooLarge",
    "SecurityHeadersMiddleware",
    "WSGuard",
    "bind_warning",
    "chain_guards",
    "check_bind",
    "cors_middleware",
    "is_loopback",
    "make_ws_origin_guard",
    "security_middleware",
    "security_stats",
    "validate_origin",
]
