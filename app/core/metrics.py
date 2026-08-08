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
from collections import Counter as _Counter
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
# `source_format` splits browser-rendered pages from ingested documents (§15.4.5),
# which is the only way to see how much of a research run actually came out of
# spreadsheets and reports. Bounded by the formats anydoc supports (~15 values),
# and `sum by (outcome)` still yields the pre-Phase-15 series.
CRAWL_PAGES = Counter(
    "crawl_pages_total",
    "Pages returned by the crawler, by outcome and what they were read from.",
    ("outcome", "source_format"),
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


# -- MCP (Phase 14) --------------------------------------------------------
# `tool` and `label` are both bounded: nine tool names, and labels come from
# API_KEYS. `outcome` is success | error | refused -- "refused" being a call the
# rate limiter, the daily cap or the concurrency bound turned away *before* it
# spent anything, which is the series worth alerting on.
MCP_TOOL_CALLS = Counter(
    "mcp_tool_calls_total",
    "MCP tool calls, by tool, API-key label and outcome.",
    ("tool", "label", "outcome"),
)
MCP_TOOL_DURATION = Histogram(
    "mcp_tool_duration_seconds",
    "Wall-clock duration of an MCP tool call.",
    ("tool",),
)
# The check that MCP_MAX_RESULT_TOKENS survives contact with real pages. A
# histogram, not a counter: the question is the shape of the tail, not the total.
MCP_RESULT_TOKENS = Histogram(
    "mcp_result_tokens",
    "Tokens of text returned by an MCP tool call, after budgeting (§3).",
    ("tool",),
    buckets=(100, 250, 500, 1000, 2000, 4000, 8000, 16000),
)
MCP_ARTIFACTS_STORED = Gauge(
    "mcp_artifacts_stored",
    "Tool payloads currently held in the artifact store.",
)


def record_mcp_call(tool: str, label: str, outcome: str, duration_seconds: float) -> None:
    """One finished MCP tool call."""
    try:
        MCP_TOOL_CALLS.labels(tool, label, outcome).inc()
        MCP_TOOL_DURATION.labels(tool).observe(duration_seconds)
    except Exception:  # noqa: BLE001 - never let a metric fail a tool call
        logger.debug("Could not record MCP call metrics", exc_info=True)


def record_mcp_result_tokens(tool: str, tokens: int) -> None:
    try:
        MCP_RESULT_TOKENS.labels(tool).observe(tokens)
    except Exception:  # noqa: BLE001
        logger.debug("Could not record MCP result size", exc_info=True)


def set_mcp_artifacts(count: int) -> None:
    try:
        MCP_ARTIFACTS_STORED.set(count)
    except Exception:  # noqa: BLE001
        logger.debug("Could not sample the artifact gauge", exc_info=True)


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
    """Count an iterable of ``CrawledPage`` by outcome and source format.

    A page that never produced content (blocked, failed before a fetch) has no
    source format and is counted as ``unknown`` rather than being attributed to
    the browser it never reached.
    """
    try:
        # Aliased at the import: `Counter` here is prometheus_client's.
        tally: _Counter[tuple[str, str]] = _Counter()
        for page in pages:
            outcome = "success" if getattr(page, "success", False) else "failure"
            tally[(outcome, getattr(page, "source_format", None) or "unknown")] += 1
        for (outcome, source_format), count in tally.items():
            CRAWL_PAGES.labels(outcome, source_format).inc(count)
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
