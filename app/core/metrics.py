"""Prometheus instrumentation (§7.2).

Every metric is a **module-level singleton**: ``prometheus_client`` registers a
collector by name in a process-wide registry, so building one per request (or
per app instance) raises ``Duplicated timeseries``. Python's import cache makes
this module's body run exactly once per process, which is precisely the lifetime
these objects need.

The rest of the codebase never imports ``prometheus_client``; it calls the small
functions below. Two reasons: the call sites stay readable (``record_cache(
"search", hit=True)``), and a deployment that wanted metrics gone would have one
file to stub instead of a dozen.

**Nothing here may raise.** A counter is an observation about a request, not part
of serving it, so every entry point swallows its own errors and logs at debug
level. The same rule the cost ledger follows in `app.api.deps.record_spend`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

logger = logging.getLogger("unified_search_api.metrics")

# Label used when no route matched (404s, and anything that never reached the
# router). A raw path here would let a scanner mint an unbounded number of time
# series just by requesting random URLs.
UNMATCHED_ROUTE = "unmatched"

# HTTP methods folded into a single label value. Same reasoning: the method comes
# from the client and is not otherwise bounded.
_KNOWN_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)
_OTHER_METHOD = "OTHER"

# -- HTTP ------------------------------------------------------------------
# `route` is the route *template* (/v1/jobs/{job_id}), never the raw path.
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Wall-clock duration of an HTTP request, including streamed response bodies.",
    ("method", "route", "status_code"),
)
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests served, by method, route template and status code.",
    ("method", "route", "status_code"),
)

# -- Caches ----------------------------------------------------------------
CACHE_REQUESTS = Counter(
    "cache_requests_total",
    "Lookups in an in-process cache, by cache name and outcome.",
    ("cache", "outcome"),
)

# -- Crawler ---------------------------------------------------------------
CRAWL_PAGES = Counter(
    "crawl_pages_total",
    "Pages returned by the crawler, by outcome.",
    ("outcome",),
)
BROWSER_SESSIONS_ACTIVE = Gauge(
    "crawler_browser_sessions_active",
    "Browser sessions currently holding a slot in the crawler's concurrency pool.",
)

# -- Jobs ------------------------------------------------------------------
# Sampled at scrape time from JobManager rather than tracked through every state
# transition: a recomputed count cannot drift, an incremented one can.
JOBS_ACTIVE = Gauge(
    "jobs_active",
    "Background jobs that have not reached a terminal state, by type.",
    ("type",),
)

# -- LLM spend (§6.5, per API-key label) -----------------------------------
LLM_TOKENS = Counter(
    "llm_tokens_total",
    "LLM tokens billed to an API-key label, by kind.",
    ("label", "kind"),
)
LLM_COST = Counter(
    "llm_cost_usd_total",
    "Estimated LLM spend in USD, by API-key label.",
    ("label",),
)
RERANK_SKIPPED_PAGES = Counter(
    "research_pages_skipped_by_rerank_total",
    "Pages the pre-crawl reranker dropped before they were crawled.",
)


def normalize_method(method: str) -> str:
    method = (method or "").upper()
    return method if method in _KNOWN_METHODS else _OTHER_METHOD


def record_request(method: str, route: str, status_code: int, duration_seconds: float) -> None:
    """One request, one duration observation."""
    try:
        labels = (normalize_method(method), route or UNMATCHED_ROUTE, str(status_code))
        HTTP_REQUESTS.labels(*labels).inc()
        HTTP_REQUEST_DURATION.labels(*labels).observe(duration_seconds)
    except Exception:  # noqa: BLE001 - never let a metric fail a request
        logger.debug("Could not record HTTP metrics", exc_info=True)


def record_cache(cache: str, *, hit: bool) -> None:
    """A lookup in the ``search`` / ``answer`` / ``rerank`` cache."""
    try:
        CACHE_REQUESTS.labels(cache, "hit" if hit else "miss").inc()
    except Exception:  # noqa: BLE001
        logger.debug("Could not record cache metrics", exc_info=True)


def record_crawled_pages(pages: Any) -> None:
    """Count an iterable of ``CrawledPage`` by ``success``."""
    try:
        success = sum(1 for page in pages if getattr(page, "success", False))
        total = len(pages)
        if success:
            CRAWL_PAGES.labels("success").inc(success)
        if total - success:
            CRAWL_PAGES.labels("failure").inc(total - success)
    except Exception:  # noqa: BLE001
        logger.debug("Could not record crawl metrics", exc_info=True)


def browser_session_started() -> None:
    try:
        BROWSER_SESSIONS_ACTIVE.inc()
    except Exception:  # noqa: BLE001
        logger.debug("Could not increment the browser-session gauge", exc_info=True)


def browser_session_finished() -> None:
    try:
        BROWSER_SESSIONS_ACTIVE.dec()
    except Exception:  # noqa: BLE001
        logger.debug("Could not decrement the browser-session gauge", exc_info=True)


# Job types seen at least once. A type that drops back to zero active jobs must
# still be published as 0, otherwise the gauge keeps reporting its last value
# forever and every dashboard shows a phantom running job.
_seen_job_types: set[str] = set()


def set_jobs_active(counts: Mapping[str, int]) -> None:
    """Publish the non-terminal job count per type, sampled at scrape time."""
    try:
        _seen_job_types.update(counts)
        for job_type in _seen_job_types:
            JOBS_ACTIVE.labels(job_type).set(counts.get(job_type, 0))
    except Exception:  # noqa: BLE001
        logger.debug("Could not sample the jobs gauge", exc_info=True)


# Totals already exported for a given UsageTracker, stashed on the tracker
# itself. `record_llm_usage` is called once per run today, but a caller that
# reported the same tracker twice would otherwise double every counter; taking
# the delta makes that harmless.
_EXPORTED_ATTR = "_metrics_exported"
_ZERO = (0, 0, 0.0, 0)


def record_llm_usage(label: str, usage: Any) -> None:
    """Export a finished run's tokens, cost and rerank savings for ``label`` (§6.5)."""
    try:
        totals = (
            int(usage.prompt_tokens),
            int(usage.completion_tokens),
            float(usage.total_cost_usd),
            int(usage.pages_skipped_by_rerank),
        )
        previous = getattr(usage, _EXPORTED_ATTR, _ZERO)
        setattr(usage, _EXPORTED_ATTR, totals)
        prompt, completion, cost, skipped = (
            max(now - before, 0) for now, before in zip(totals, previous, strict=True)
        )
        if prompt:
            LLM_TOKENS.labels(label, "prompt").inc(prompt)
        if completion:
            LLM_TOKENS.labels(label, "completion").inc(completion)
        if cost > 0:
            LLM_COST.labels(label).inc(cost)
        if skipped:
            RERANK_SKIPPED_PAGES.inc(skipped)
    except Exception:  # noqa: BLE001
        logger.debug("Could not record LLM usage metrics", exc_info=True)


def render() -> tuple[bytes, str]:
    """The exposition payload and its content type, for ``GET /metrics``."""
    return generate_latest(), CONTENT_TYPE_LATEST
