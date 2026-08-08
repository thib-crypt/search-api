"""stdio transport: ``python -m app.mcp`` (§8).

Same `MCPServer`, same nine tools, same services — a different pipe. It exists
because a good part of the desktop client population still speaks only stdio, and
telling those users to stand up a reverse proxy to try the thing is a bad first
impression.

**This is a workstation mode, not a server mode.** The process is one client's:
it starts its own Chromium pool, its own SearXNG connection pool and its own
LLM client, so `CRAWL_MAX_CONCURRENCY` bounds *this* process and not the fleet.
Run one next to a deployed instance and you have two browser pools, not one
shared budget. The rate limiter is skipped for the same reason — there is no
shared window to draw from over a pipe — while `DAILY_COST_CAP_USD` still
applies, since it is enforced on the ledger and a SQLite ledger is shared.

Configuration comes from the same `.env` as the HTTP app, so the model, the
SearXNG URL and the crawl guards are whatever the deployment already uses.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace

from app.config import get_settings
from app.core.cache import TTLCache
from app.core.cost import build_cost_ledger
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.logging import configure_logging
from app.core.net.guard import UrlGuard
from app.core.research.rerank import build_reranker
from app.core.search.searxng import SearxngClient
from app.jobs.manager import JobManager
from app.jobs.store import build_job_store
from app.mcp.artifacts import ArtifactStore
from app.mcp.guards import ConcurrencyBound
from app.mcp.server import build_server

logger = logging.getLogger("unified_search_api.mcp")


def _logs_to_stderr() -> None:
    """Move every stream handler in the process off stdout.

    Under stdio, **stdout is the transport**: one log line written there is a
    corrupt JSON-RPC frame, and the client's parser will not recover politely.
    `configure_logging` targets stdout, which is right for a container (one
    stream, one shipper) and fatal here — and litellm attaches handlers of its
    own on import, so retargeting the root logger alone is not enough.

    The SDK's stdio transport does divert file descriptor 1 once it starts, but
    that leaves everything logged during startup — client construction, the
    litellm router banner — going straight down the wire before the diversion
    exists. This runs first.
    """
    manager = logging.getLogger().manager
    loggers: list[logging.Logger] = [logging.getLogger()]
    loggers += [
        candidate
        for candidate in manager.loggerDict.values()
        if isinstance(candidate, logging.Logger)
    ]
    for logger_ in loggers:
        for handler in logger_.handlers:
            if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
                handler.setStream(sys.stderr)


async def _serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, "text")
    _logs_to_stderr()

    url_guard = UrlGuard.from_settings(settings)
    searxng = SearxngClient(
        base_url=settings.searxng_url,
        timeout=settings.searxng_timeout,
        cache_ttl=settings.search_cache_ttl if settings.search_cache_enabled else 0.0,
        cache_max_size=settings.search_cache_max_size,
        max_connections=settings.searxng_max_connections,
    )
    crawler = CrawlerService(
        max_concurrency=settings.crawl_max_concurrency,
        timeout=settings.crawl_timeout,
        guard=url_guard,
        per_host_delay=settings.crawl_per_host_delay,
        enable_documents=settings.documents_enabled,
        doc_max_mb=settings.doc_max_mb,
        pdf_fallback=settings.crawl_pdf_fallback,
    )
    llm = LLMClient(
        default_model=settings.llm_model,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        fallback_models=settings.llm_fallback_model_list,
        timeout=settings.llm_timeout,
        stream_timeout=settings.llm_stream_timeout,
        max_retries=settings.llm_max_retries,
        max_concurrency=settings.llm_max_concurrency,
    )
    cost_ledger = build_cost_ledger(settings.job_backend, settings.job_db_path)
    await cost_ledger.start()
    jobs = JobManager(
        retention_seconds=settings.job_retention_seconds,
        evict_interval_seconds=settings.job_evict_interval_seconds,
        max_progress_events=settings.job_max_progress_events,
        subscriber_queue_size=settings.job_subscriber_queue_size,
        store=build_job_store(settings.job_backend, settings.job_db_path),
        vacuum_interval_seconds=settings.job_vacuum_interval_seconds,
    )
    await jobs.start()

    # Duck-typed stand-in for `app.state`: `state_of` reads attributes, and
    # importing FastAPI to build a real one would buy nothing.
    state = SimpleNamespace(
        url_guard=url_guard,
        searxng=searxng,
        crawler=crawler,
        llm=llm,
        reranker=build_reranker(
            settings.research_rerank,
            llm=llm,
            model=settings.research_rerank_model,
            cache_ttl=settings.research_rerank_cache_ttl,
        ),
        answer_cache=(
            TTLCache(ttl=settings.answer_cache_ttl, max_size=settings.answer_cache_max_size)
            if settings.answer_cache_enabled and settings.answer_cache_ttl > 0
            else None
        ),
        cost_ledger=cost_ledger,
        jobs=jobs,
        mcp_artifacts=ArtifactStore(
            ttl=settings.mcp_artifact_ttl, max_items=settings.mcp_artifact_max_items
        ),
        mcp_research_bound=ConcurrencyBound(limit=settings.mcp_research_max_concurrent),
    )

    server = build_server(state)
    try:
        await server.run_stdio_async()
    finally:
        await jobs.shutdown()
        close = getattr(cost_ledger, "close", None)
        if close is not None:
            await close()
        await searxng.aclose()
        await crawler.close()


def main() -> None:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:  # pragma: no cover - a client closing the pipe
        pass


if __name__ == "__main__":
    main()
