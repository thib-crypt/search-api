"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.middleware import RateLimitMiddleware, RequestContextMiddleware
from app.api.routes import answer, crawl, health, jobs, research, search
from app.config import get_settings
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.search.searxng import SearxngClient
from app.jobs.manager import JobManager

logger = logging.getLogger("unified_search_api")

settings = get_settings()
logging.basicConfig(level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Shared async clients live on app.state for the process lifetime.
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
        max_concurrency=settings.crawl_max_concurrency, timeout=settings.crawl_timeout
    )
    app.state.llm = LLMClient(
        default_model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
    app.state.jobs = JobManager(retention_seconds=settings.job_retention_seconds)
    try:
        yield
    finally:
        # Cancel in-flight jobs before tearing down the clients they depend on.
        await app.state.jobs.shutdown()
        await app.state.searxng.aclose()
        await app.state.crawler.close()


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description=(
        "Self-hosted unified API for web search (SearXNG), crawling (crawl4ai) "
        "and LLM deep-research reports."
    ),
    lifespan=lifespan,
)

# Middleware runs in reverse registration order, so the rate limiter (added last)
# executes first, then the request-context/security-headers layer, then CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(RateLimitMiddleware)


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
            "/v1/answer",
            "/v1/research",
            "/v1/jobs/{id}",
        ],
    }


app.include_router(health.router)
app.include_router(search.router)
app.include_router(crawl.router)
app.include_router(answer.router)
app.include_router(research.router)
app.include_router(jobs.router)
