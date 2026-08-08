"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.routing import Route

from app import __version__
from app.api.middleware import (
    CompressionMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
)
from app.api.routes import (
    answer,
    convert,
    crawl,
    extract,
    health,
    jobs,
    metrics,
    research,
    search,
)
from app.api.routes import map as map_routes  # aliased: `map` shadows the builtin
from app.config import get_settings
from app.core.cache import TTLCache
from app.core.cost import build_cost_ledger
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.logging import configure_logging
from app.core.net.guard import UrlGuard
from app.core.research.rerank import build_reranker
from app.core.search.searxng import SearxngClient
from app.core.tracing import setup_tracing
from app.jobs.manager import JobManager
from app.jobs.store import build_job_store
from app.mcp.artifacts import ArtifactStore
from app.mcp.guards import ConcurrencyBound
from app.mcp.server import build_http_app, build_server, mcp_endpoint

logger = logging.getLogger("unified_search_api")

settings = get_settings()
# §7.1: JSON on one line per record by default, with the request id stamped on
# every record emitted while a request is in flight. LOG_FORMAT=text restores the
# previous human-readable format for a local run.
configure_logging(settings.log_level, settings.log_format)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Read the settings again at startup rather than reusing the import-time copy,
    # so the running app reflects the environment it was actually started in.
    settings = get_settings()
    # Refuse to serve a configuration that would leave the API (and the provider
    # bill behind it) open. Raising here aborts startup.
    settings.validate_startup()
    # Re-read here too: the import-time call above used the import-time settings.
    configure_logging(settings.log_level, settings.log_format)
    # §7.3: opt-in, best-effort. Never aborts startup.
    if settings.otel_enabled:
        setup_tracing(app, service_name=settings.app_name)
    # §15.7: the renamed crawl settings still work, but say so once at startup —
    # silently honouring a name that is on its way out is how a rename turns into
    # a surprise two versions later.
    for warning in settings.deprecation_warnings():
        logger.warning("%s", warning)
    if not settings.auth_enabled and settings.environment != "development":
        logger.warning(
            "AUTH_ENABLED is false in environment=%s: every caller can spend your "
            "LLM credits through /v1/research. Set AUTH_ENABLED=true and API_KEYS.",
            settings.environment,
        )

    # Shared async clients live on app.state for the process lifetime.
    app.state.url_guard = UrlGuard.from_settings(settings)
    app.state.searxng = SearxngClient(
        base_url=settings.searxng_url,
        timeout=settings.searxng_timeout,
        cache_ttl=settings.search_cache_ttl if settings.search_cache_enabled else 0.0,
        cache_max_size=settings.search_cache_max_size,
        max_connections=settings.searxng_max_connections,
    )
    # The browser pool starts lazily on the first crawl, so creating the service
    # here is cheap and keeps startup fast.
    app.state.crawler = CrawlerService(
        max_concurrency=settings.crawl_max_concurrency,
        timeout=settings.crawl_timeout,
        guard=app.state.url_guard,
        per_host_delay=settings.crawl_per_host_delay,
        enable_documents=settings.documents_enabled,
        doc_max_mb=settings.doc_max_mb,
        pdf_fallback=settings.crawl_pdf_fallback,
    )
    app.state.llm = LLMClient(
        default_model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        fallback_models=settings.llm_fallback_model_list,
        timeout=settings.llm_timeout,
        stream_timeout=settings.llm_stream_timeout,
        max_retries=settings.llm_max_retries,
        max_concurrency=settings.llm_max_concurrency,
    )
    # §5.7: shared by every request; `AnswerService` itself is built per request.
    app.state.answer_cache = (
        TTLCache(ttl=settings.answer_cache_ttl, max_size=settings.answer_cache_max_size)
        if settings.answer_cache_enabled and settings.answer_cache_ttl > 0
        else None
    )
    # Chosen once from config: the engine only ever sees the protocol.
    app.state.reranker = build_reranker(
        settings.research_rerank,
        llm=app.state.llm,
        model=settings.research_rerank_model,
        cache_ttl=settings.research_rerank_cache_ttl,
    )
    # §6.4b: the per-key daily spend counters. Durable alongside the jobs when
    # JOB_BACKEND=sqlite, process-local (and reset on restart) otherwise.
    app.state.cost_ledger = build_cost_ledger(settings.job_backend, settings.job_db_path)
    await app.state.cost_ledger.start()
    app.state.jobs = JobManager(
        retention_seconds=settings.job_retention_seconds,
        evict_interval_seconds=settings.job_evict_interval_seconds,
        max_progress_events=settings.job_max_progress_events,
        subscriber_queue_size=settings.job_subscriber_queue_size,
        # §6.1: finished reports survive a restart instead of 404ing.
        store=build_job_store(settings.job_backend, settings.job_db_path),
        vacuum_interval_seconds=settings.job_vacuum_interval_seconds,
    )
    await app.state.jobs.start()

    # §3: full tool payloads behind the truncated results MCP hands back. In
    # memory and bounded, like every other cache here.
    app.state.mcp_artifacts = ArtifactStore(
        ttl=settings.mcp_artifact_ttl, max_items=settings.mcp_artifact_max_items
    )
    # §6.3: the agent-specific guard rail. The daily cap stops the spending; this
    # stops a loop from reaching the cap in thirty seconds.
    app.state.mcp_research_bound = ConcurrencyBound(limit=settings.mcp_research_max_concurrent)
    app.state.mcp_app = None
    async with AsyncExitStack() as stack:
        if settings.mcp_enabled:
            # Built here, not at import: a StreamableHTTPSessionManager may only
            # be run once, so a process that starts the app twice (every test
            # that opens a TestClient does) needs a fresh server each time. The
            # route registered below resolves this through app.state.
            mcp_server = build_server(app.state)
            app.state.mcp_app = build_http_app(mcp_server, settings)
            # The mounted app's own lifespan never runs — Starlette only runs the
            # outermost one — so its session manager is started from here.
            await stack.enter_async_context(mcp_server.session_manager.run())
            logger.info("MCP server mounted on %s", settings.mcp_path)
        try:
            yield
        finally:
            # Cancel in-flight jobs before tearing down the clients they depend on.
            await app.state.jobs.shutdown()
            close_ledger = getattr(app.state.cost_ledger, "close", None)
            if close_ledger is not None:
                await close_ledger()
            await app.state.searxng.aclose()
            await app.state.crawler.close()
            app.state.mcp_app = None


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description=(
        "Self-hosted unified API for web search (SearXNG), crawling (crawl4ai) "
        "and LLM deep-research reports."
    ),
    lifespan=lifespan,
    # NOTE (§3.3): deliberately no `default_response_class=ORJSONResponse` here.
    # On FastAPI >= 0.136 a route serializes straight to JSON bytes via Pydantic's
    # Rust core only when its response class is still the framework default
    # (`use_dump_json = response_field is not None and isinstance(response_class,
    # DefaultPlaceholder)` in fastapi/routing.py). Setting the app-wide default to
    # ORJSONResponse makes it a non-placeholder and silently turns that fast path
    # off for *every* route that declares a response model or a return annotation
    # -- crawl, search, research, jobs, health -- replacing it with the slower
    # "validate -> Python dict -> orjson.dumps" path. orjson is therefore applied
    # per-route, only where the fast path is unavailable: see /v1/answer, whose
    # union with EventSourceResponse forces response_model=None.
)

# Middleware runs in reverse registration order, so compression (added last)
# executes first, then the rate limiter, then the request-context/security-headers
# layer, then CORS. Compression sits outermost on purpose: it is the last layer to
# touch the response on the way out, so it compresses the final body after CORS and
# the security headers have been added. It leaves text/event-stream untouched.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
    # §6.5: `allow_headers=["*"]` already covers the Mcp-* request headers, but a
    # browser-based MCP client cannot *read* a response header it was not granted.
    # Without this, such a client never sees the session id the streamable-HTTP
    # transport issues, so it cannot hold a session at all.
    expose_headers=["Mcp-Session-Id", "Mcp-Protocol-Version", "X-Request-ID"],
)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(CompressionMiddleware, minimum_size=1024)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    logger.exception("Unhandled error on %s (request_id=%s)", request.url.path, request_id)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error.", "request_id": request_id},
    )


@app.get("/", tags=["meta"], summary="API metadata")
async def root() -> dict[str, object]:
    return {
        "name": settings.app_name,
        "version": __version__,
        "docs": "/docs",
        "endpoints": [
            "/v1/search",
            "/v1/crawl",
            "/v1/crawl/site",
            "/v1/extract",
            "/v1/map",
            "/v1/convert",
            "/v1/answer",
            "/v1/research",
            "/v1/jobs/{id}",
        ],
        # Phase 14: the same capabilities as MCP tools, for agent clients.
        "mcp": settings.mcp_path if settings.mcp_enabled else None,
    }


app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(search.router)
app.include_router(crawl.router)
app.include_router(extract.router)
app.include_router(map_routes.router)
app.include_router(convert.router)
app.include_router(answer.router)
app.include_router(research.router)
app.include_router(jobs.router)

# The MCP transport, at exactly `MCP_PATH` — a plain Starlette route rather than
# `app.mount()`. A Mount only matches `MCP_PATH + "/"`, so every client
# configured with `https://host/mcp` would collect a 307 before each request.
# The endpoint is a shim (`mcp_endpoint`) because the app behind it is rebuilt on
# each startup; it answers 404 when MCP_ENABLED is false, like any unknown path.
app.router.routes.append(
    Route(settings.mcp_path, endpoint=mcp_endpoint, methods=["GET", "POST", "DELETE"])
)
