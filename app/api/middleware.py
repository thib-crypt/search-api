"""HTTP hardening middleware: request IDs, security headers, rate limiting.

All middleware read settings dynamically per request so behavior can be toggled
via the environment without re-importing the app.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings

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


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request id and (optionally) security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid4().hex
        request.state.request_id = request_id

        response: Response = await call_next(request)

        response.headers["X-Request-ID"] = request_id
        if get_settings().security_headers:
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers.setdefault("Cache-Control", "no-store")
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple per-IP sliding-window limiter, enabled via RATE_LIMIT_ENABLED."""

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        if not settings.rate_limit_enabled or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        if settings.trusted_proxy:
            forwarded_for = request.headers.get("x-forwarded-for")
            client_ip = (
                forwarded_for.split(",")[0].strip()
                if forwarded_for
                else (request.client.host if request.client else "unknown")
            )
        else:
            client_ip = request.client.host if request.client else "unknown"
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
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded."},
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)
        return await call_next(request)
