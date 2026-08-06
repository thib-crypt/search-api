"""HTTP hardening middleware: request IDs, security headers, rate limiting, compression.

All middleware read settings dynamically per request so behavior can be toggled
via the environment without re-importing the app.

These classes are pure ASGI3 middleware rather than ``BaseHTTPMiddleware``
subclasses: the latter wraps every request in an anyio task group plus a memory
object stream, which costs on the critical path of a single-worker deployment
and breaks backpressure on the SSE endpoints (``GET /v1/jobs/{id}/stream`` and
the streaming branch of ``POST /v1/answer``).
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import MutableMapping
from typing import Any
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware

from app.config import get_settings

Scope = MutableMapping[str, Any]

# Paths that should never be rate limited.
_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

# Per-client sliding window of request timestamps (monotonic seconds).
# An OrderedDict keyed in insertion order so the coldest client is evicted first
# when the table hits its cap. Plain dict growth was unbounded: every IP ever
# seen kept an entry for the process lifetime, which a distributed client (or
# any IPv6 traffic) turns into a slow leak.
_HITS: OrderedDict[str, deque[float]] = OrderedDict()
_WINDOW = 60.0
# How often the stale-entry sweep runs, at most.
_PURGE_INTERVAL = 60.0
_last_purge = 0.0


def reset_rate_limiter() -> None:
    """Clear all rate-limit state (used by tests)."""
    global _last_purge
    _HITS.clear()
    _last_purge = 0.0


def _purge(now: float, max_clients: int) -> None:
    """Drop windows with no recent hits, then cap the table size."""
    global _last_purge
    if now - _last_purge >= _PURGE_INTERVAL:
        _last_purge = now
        for client in [c for c, hits in _HITS.items() if not hits or now - hits[-1] > _WINDOW]:
            del _HITS[client]
    while len(_HITS) > max_clients:
        _HITS.popitem(last=False)


def _header(scope: Scope, name: str) -> str | None:
    """Case-insensitive lookup of a request header from the raw ASGI scope."""
    wanted = name.lower().encode("latin-1")
    for key, value in scope["headers"]:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


class RequestContextMiddleware:
    """Attach a request id and (optionally) security headers to every response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _header(scope, "x-request-id") or uuid4().hex
        # Starlette's Request.state reads and writes through scope["state"], so
        # downstream handlers still see request.state.request_id.
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                if get_settings().security_headers:
                    headers["X-Content-Type-Options"] = "nosniff"
                    headers["X-Frame-Options"] = "DENY"
                    headers["Referrer-Policy"] = "no-referrer"
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_wrapper)


# Byte-for-byte identical to what JSONResponse would have produced.
_RATE_LIMIT_BODY = b'{"detail":"Rate limit exceeded."}'


class RateLimitMiddleware:
    """Simple per-IP sliding-window limiter, enabled via RATE_LIMIT_ENABLED."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        if not settings.rate_limit_enabled or scope["path"] in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        if settings.trusted_proxy:
            forwarded_for = _header(scope, "x-forwarded-for")
            client_ip = (
                forwarded_for.split(",")[0].strip()
                if forwarded_for
                else (client[0] if client else "unknown")
            )
        else:
            client_ip = client[0] if client else "unknown"
        now = time.monotonic()
        _purge(now, settings.rate_limit_max_clients)
        hits = _HITS.get(client_ip)
        if hits is None:
            hits = _HITS[client_ip] = deque()
        else:
            _HITS.move_to_end(client_ip)
        while hits and now - hits[0] > _WINDOW:
            hits.popleft()

        if len(hits) >= settings.rate_limit_rpm:
            retry_after = int(_WINDOW - (now - hits[0])) + 1
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"retry-after", str(retry_after).encode("latin-1")),
                        (b"content-length", str(len(_RATE_LIMIT_BODY)).encode("latin-1")),
                        (b"content-type", b"application/json"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _RATE_LIMIT_BODY})
            return

        hits.append(now)
        await self.app(scope, receive, send)


def _is_event_stream(headers) -> bool:
    """True when raw ASGI response headers announce ``text/event-stream``."""
    for key, value in headers:
        if key.lower() == b"content-type":
            return value.lower().lstrip().startswith(b"text/event-stream")
    return False


class CompressionMiddleware:
    """GZip large JSON/markdown payloads, never Server-Sent Events.

    Crawl responses of many pages and research reports are worth compressing;
    SSE is not. Starlette's ``GZipMiddleware`` buffers the body it encodes,
    which would destroy incremental delivery and backpressure on the streaming
    endpoints. Path-based exclusion is not enough: ``POST /v1/answer`` picks SSE
    from the *request body* (``stream=true``), so the decision has to be made on
    the outgoing ``Content-Type``.

    The first ``http.response.start`` is inspected; if it announces
    ``text/event-stream``, that message and every one after it bypasses the gzip
    responder and goes straight to the server untouched. Everything else is
    handed to a stock ``GZipMiddleware`` so the compression itself (streaming
    chunks, ``Content-Length``/``Vary`` bookkeeping) stays Starlette's code.
    """

    def __init__(self, app, minimum_size: int = 1024) -> None:
        self.app = app
        self.minimum_size = minimum_size

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def inner(inner_scope: Scope, inner_receive, gzip_send) -> None:
            # `gzip_send` belongs to GZipMiddleware's responder; `send` is the
            # raw server send. SSE responses are routed around the responder.
            passthrough = False

            async def send_wrapper(message) -> None:
                nonlocal passthrough
                if message["type"] == "http.response.start":
                    passthrough = _is_event_stream(message.get("headers", ()))
                await (send if passthrough else gzip_send)(message)

            await self.app(inner_scope, inner_receive, send_wrapper)

        # One GZipMiddleware per request: it only holds config, and its responder
        # allocates a fresh gzip buffer per request anyway.
        await GZipMiddleware(inner, minimum_size=self.minimum_size)(scope, receive, send)
