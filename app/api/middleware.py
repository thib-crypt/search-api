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

import logging
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, MutableMapping
from typing import Any
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Send

from app.api.deps import matched_label, presented_key
from app.config import Settings, get_settings
from app.core import metrics
from app.core.logging import request_id_var

Scope = MutableMapping[str, Any]
# Raw ASGI header pairs, as they appear in a scope or an `http.response.start`.
RawHeaders = Iterable[tuple[bytes, bytes]]

access_logger = logging.getLogger("unified_search_api.access")

# Paths that should never be rate limited. Both health probes are here: an
# orchestrator polling readiness every second must not consume a caller's budget
# (nor be throttled into declaring the instance dead). `/metrics` too: a Prometheus
# scrape runs on its own schedule, far more often than a real client request, and
# must neither spend a caller's budget nor be throttled into gaps in the series.
_EXEMPT_PATHS = {"/health", "/health/ready", "/docs", "/openapi.json", "/redoc", "/metrics"}

# §6.3: what each endpoint costs, in points. A /v1/research call is ~1000x a
# /v1/search in provider spend; counting both as "one request" means a limit of
# 120/min authorizes 120 deep research runs a minute, which is a drained budget.
# Matched by prefix on the raw ASGI path — this middleware runs before routing,
# so there is no route object to consult. Longest prefix wins, which is what
# keeps /v1/crawl/site from being priced as /v1/crawl.
_WEIGHTS: tuple[tuple[str, int], ...] = tuple(
    sorted(
        (
            ("/v1/search", 1),
            ("/v1/map", 2),
            ("/v1/crawl", 5),
            ("/v1/answer", 10),
            ("/v1/extract", 10),
            ("/v1/crawl/site", 25),
            ("/v1/research", 100),
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)
# Anything else that is not exempt (/v1/jobs/*, /, ...) costs this.
_DEFAULT_WEIGHT = 1

# Per-caller sliding window of (timestamp, weight) pairs, monotonic seconds.
# An OrderedDict keyed in insertion order so the coldest client is evicted first
# when the table hits its cap. Plain dict growth was unbounded: every IP ever
# seen kept an entry for the process lifetime, which a distributed client (or
# any IPv6 traffic) turns into a slow leak.
_HITS: OrderedDict[str, deque[tuple[float, int]]] = OrderedDict()
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
        for client in [c for c, hits in _HITS.items() if not hits or now - hits[-1][0] > _WINDOW]:
            del _HITS[client]
    while len(_HITS) > max_clients:
        _HITS.popitem(last=False)


def _header(scope: Scope, name: str) -> str | None:
    """Case-insensitive lookup of a request header from the raw ASGI scope."""
    wanted = name.lower().encode("latin-1")
    headers: RawHeaders = scope["headers"]
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


# id(endpoint) -> (endpoint, route template). Fallback index for the case where
# the matched route object is not in the scope (plain Starlette routes put only
# `endpoint` and `path_params` there). The endpoint is kept alive by this table
# so its id can never be recycled onto a different object.
_ROUTE_TEMPLATES: dict[int, tuple[Any, str]] = {}


def _index_routes(node: Any) -> None:
    """Walk the route tree, recording ``endpoint -> path template`` pairs.

    Recursive because `include_router` keeps sub-routers as nodes in
    ``app.routes`` rather than flattening their routes into it.
    """
    for route in getattr(node, "routes", ()) or ():
        endpoint = getattr(route, "endpoint", None)
        template = getattr(route, "path", None)
        if endpoint is not None and isinstance(template, str):
            _ROUTE_TEMPLATES.setdefault(id(endpoint), (endpoint, template))
        elif getattr(route, "routes", None):
            _index_routes(route)


def route_template(scope: Scope) -> str:
    """The matched route's *template*, e.g. ``/v1/jobs/{job_id}``.

    Never the raw path: a Prometheus label built from ``/v1/jobs/<uuid>/stream``
    would mint one time series per job and keep it for the process lifetime.
    Only meaningful once routing has happened, i.e. after the inner app returns.
    """
    template = getattr(scope.get("route"), "path", None)
    if isinstance(template, str):
        return template
    endpoint = scope.get("endpoint")
    if endpoint is None:  # no route matched (404), or the request never got that far
        return metrics.UNMATCHED_ROUTE
    entry = _ROUTE_TEMPLATES.get(id(endpoint))
    if entry is None:
        _index_routes(scope.get("app"))
        entry = _ROUTE_TEMPLATES.get(id(endpoint))
    return entry[1] if entry is not None else metrics.UNMATCHED_ROUTE


class RequestContextMiddleware:
    """Request id, security headers, access log and per-request metrics.

    The timing wraps the whole ``await self.app(...)`` rather than stopping at
    ``http.response.start``: that message only says when the *headers* were
    decided, so an SSE response would otherwise be recorded as taking a few
    milliseconds while its body streamed for minutes.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _header(scope, "x-request-id") or uuid4().hex
        # Starlette's Request.state reads and writes through scope["state"], so
        # downstream handlers still see request.state.request_id.
        scope.setdefault("state", {})["request_id"] = request_id
        # §7.1: makes the id visible to every logger.* call in this request's call
        # tree without threading it through a single function signature. Reset in
        # the `finally` so it cannot leak into whatever the worker runs next.
        token = request_id_var.set(request_id)

        # Only overwritten if a response actually starts; a request that dies in
        # the ASGI stack is recorded as the 500 the server will send.
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                if get_settings().security_headers:
                    headers["X-Content-Type-Options"] = "nosniff"
                    headers["X-Frame-Options"] = "DENY"
                    headers["Referrer-Policy"] = "no-referrer"
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                _observe(scope, status_code, time.perf_counter() - started)
            finally:
                request_id_var.reset(token)


def _observe(scope: Scope, status_code: int, elapsed: float) -> None:
    """Emit the access log line and the HTTP metrics for one finished request.

    Swallows its own failures: observability is not allowed to turn a served
    response into an error.
    """
    method = scope.get("method", "")
    try:
        access_logger.info(
            "%s %s %s %.1fms",
            method,
            scope.get("path", ""),
            status_code,
            elapsed * 1000,
            extra={
                # The raw path here, on purpose: a log line has no cardinality
                # budget, and knowing *which* job was streamed is the point.
                "method": method,
                "path": scope.get("path", ""),
                "status_code": status_code,
                "duration_ms": round(elapsed * 1000, 3),
            },
        )
    except Exception:  # noqa: BLE001 - a logging failure must not fail the request
        pass
    metrics.record_request(method, route_template(scope), status_code, elapsed)


# Byte-for-byte identical to what JSONResponse would have produced.
_RATE_LIMIT_BODY = b'{"detail":"Rate limit exceeded."}'


def path_weight(path: str) -> int:
    """Points charged for a request to ``path`` (§6.3)."""
    for prefix, weight in _WEIGHTS:
        if path.startswith(prefix):
            return weight
    return _DEFAULT_WEIGHT


def _client_ip(scope: Scope, trusted_proxy: bool) -> str:
    client = scope.get("client")
    if trusted_proxy:
        forwarded_for = _header(scope, "x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return client[0] if client else "unknown"


def _bucket(scope: Scope, settings: Settings) -> tuple[str, str | None]:
    """The window this request draws from: ``(bucket key, label or None)``.

    §6.3 keys the budget on the API-key **label** when auth is on, so one
    client's traffic cannot eat another's. This middleware runs before routing,
    hence before `require_api_key`, so it resolves the label itself from the same
    two headers — using the same constant-time comparison, and dropping the key
    the instant the label is known. A caller with no key or a bad one (which the
    route will 401 anyway) falls back to its IP, so key-guessing floods are still
    bounded.
    """
    if settings.auth_enabled:
        provided = presented_key(_header(scope, "authorization"), _header(scope, "x-api-key"))
        label = matched_label(provided, settings.api_key_labels) if provided else None
        if label is not None:
            return f"label:{label}", label
    return f"ip:{_client_ip(scope, settings.trusted_proxy)}", None


class RateLimitMiddleware:
    """Weighted sliding-window limiter (§6.3), enabled via RATE_LIMIT_ENABLED.

    Two things distinguish it from a plain request counter: a request costs
    points according to what the endpoint spends downstream, and the window is
    keyed on the caller's API-key label rather than its IP whenever auth is on.

    Still an in-memory structure, and still correct for the single-worker
    deployment of §0: there is exactly one process holding the counters.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        if not settings.rate_limit_enabled or scope["path"] in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        key, label = _bucket(scope, settings)
        limit = settings.rate_limit_overrides.get(label or "", settings.rate_limit_rpm)
        weight = path_weight(scope["path"])

        now = time.monotonic()
        _purge(now, settings.rate_limit_max_clients)
        hits = _HITS.get(key)
        if hits is None:
            hits = _HITS[key] = deque()
        else:
            _HITS.move_to_end(key)
        while hits and now - hits[0][0] > _WINDOW:
            hits.popleft()

        spent = sum(w for _, w in hits)
        # Reset is when the window's oldest point expires, i.e. when budget frees up.
        reset = int(_WINDOW - (now - hits[0][0])) + 1 if hits else int(_WINDOW)

        if spent + weight > limit:
            headers = [
                (b"retry-after", str(reset).encode("latin-1")),
                (b"content-length", str(len(_RATE_LIMIT_BODY)).encode("latin-1")),
                (b"content-type", b"application/json"),
                *_limit_headers(limit, max(limit - spent, 0), reset),
            ]
            await send({"type": "http.response.start", "status": 429, "headers": headers})
            await send({"type": "http.response.body", "body": _RATE_LIMIT_BODY})
            return

        hits.append((now, weight))
        remaining = max(limit - (spent + weight), 0)

        async def send_wrapper(message: Message) -> None:
            # Advertised on every rate-limited response, not only on 429s: a
            # client can only pace itself if it sees the budget while it still
            # has one.
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["RateLimit-Limit"] = str(limit)
                headers["RateLimit-Remaining"] = str(remaining)
                headers["RateLimit-Reset"] = str(reset)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _limit_headers(limit: int, remaining: int, reset: int) -> list[tuple[bytes, bytes]]:
    return [
        (b"ratelimit-limit", str(limit).encode("latin-1")),
        (b"ratelimit-remaining", str(remaining).encode("latin-1")),
        (b"ratelimit-reset", str(reset).encode("latin-1")),
    ]


def _is_event_stream(headers: RawHeaders) -> bool:
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

    def __init__(self, app: ASGIApp, minimum_size: int = 1024) -> None:
        self.app = app
        self.minimum_size = minimum_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def inner(inner_scope: Scope, inner_receive: Receive, gzip_send: Send) -> None:
            # `gzip_send` belongs to GZipMiddleware's responder; `send` is the
            # raw server send. SSE responses are routed around the responder.
            passthrough = False

            async def send_wrapper(message: Message) -> None:
                nonlocal passthrough
                if message["type"] == "http.response.start":
                    passthrough = _is_event_stream(message.get("headers", ()))
                await (send if passthrough else gzip_send)(message)

            await self.app(inner_scope, inner_receive, send_wrapper)

        # One GZipMiddleware per request: it only holds config, and its responder
        # allocates a fresh gzip buffer per request anyway.
        await GZipMiddleware(inner, minimum_size=self.minimum_size)(scope, receive, send)
