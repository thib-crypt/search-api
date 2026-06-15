"""HTTP hardening middleware: request IDs, security headers, rate limiting.

All middleware read settings dynamically per request so behavior can be toggled
via the environment without re-importing the app.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings

# Paths that should never be rate limited.
_EXEMPT_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

# Per-IP sliding window of request timestamps (monotonic seconds).
_HITS: dict[str, deque[float]] = defaultdict(deque)
_WINDOW = 60.0


def reset_rate_limiter() -> None:
    """Clear all rate-limit state (used by tests)."""
    _HITS.clear()


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
        hits = _HITS[client_ip]
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
