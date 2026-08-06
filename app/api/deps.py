"""Shared FastAPI dependencies: optional API-key auth and client accessors."""

from __future__ import annotations

import hmac
import logging

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import ANONYMOUS_LABEL, Settings, get_settings
from app.core import metrics
from app.core.cache import TTLCache
from app.core.cost import CostLedger, InMemoryCostLedger
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.llm.usage import UsageTracker
from app.core.net.guard import UrlGuard
from app.core.research.rerank import NoopReranker, Reranker
from app.core.search.searxng import SearxngClient
from app.jobs.manager import JobManager
from app.models.answer import AnswerResponse

logger = logging.getLogger("unified_search_api.deps")


def _key_accepted(provided: str, keys: set[str]) -> bool:
    """Compare against every configured key in constant time.

    ``provided in keys`` short-circuits on the first differing byte, which leaks
    key material through response timing. Every candidate is compared in full,
    and the loop is not cut short on a match.
    """
    accepted = False
    for key in keys:
        if hmac.compare_digest(provided, key):
            accepted = True
    return accepted


def matched_label(provided: str, labels: dict[str, str]) -> str | None:
    """The label of whichever configured key ``provided`` equals, else ``None``.

    Same shape as `_key_accepted`: every candidate is compared in full and the
    loop is never cut short, so recovering *which* key matched costs no timing
    signal. The key itself is dropped the moment its label is known — nothing
    downstream ever receives it.
    """
    label: str | None = None
    for key, candidate in labels.items():
        if hmac.compare_digest(provided, key):
            label = candidate
    return label


def presented_key(authorization: str | None, x_api_key: str | None) -> str | None:
    """The key a request presents, from either accepted header."""
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Enforce API-key auth when AUTH_ENABLED is true; otherwise a no-op.

    The key itself is never logged, not even truncated. What *is* recorded, on
    ``request.state.api_key_label``, is the caller's label (§6.2) — the handle
    every downstream meter (rate limiting, cost attribution) uses. With auth
    disabled every caller shares the ``anonymous`` label, so that code never has
    to special-case an unauthenticated instance.
    """
    if not settings.auth_enabled:
        request.state.api_key_label = ANONYMOUS_LABEL
        return

    provided = presented_key(authorization, x_api_key)
    label = matched_label(provided, settings.api_key_labels) if provided else None

    if label is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.api_key_label = label


def get_api_key_label(request: Request) -> str:
    """The caller's label, for handlers that meter or attribute per caller."""
    return getattr(request.state, "api_key_label", None) or ANONYMOUS_LABEL


def get_searxng(request: Request) -> SearxngClient:
    client: SearxngClient | None = getattr(request.app.state, "searxng", None)
    if client is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="Search backend not initialized.")
    return client


def get_crawler(request: Request) -> CrawlerService:
    crawler: CrawlerService | None = getattr(request.app.state, "crawler", None)
    if crawler is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="Crawler not initialized.")
    return crawler


def get_llm(request: Request) -> LLMClient:
    llm: LLMClient | None = getattr(request.app.state, "llm", None)
    if llm is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="LLM client not initialized.")
    return llm


def get_reranker(request: Request) -> Reranker:
    """The reranker chosen at startup; a no-op filter if startup never ran."""
    reranker: Reranker | None = getattr(request.app.state, "reranker", None)
    if reranker is None:  # pragma: no cover - defensive
        return NoopReranker()
    return reranker


def get_url_guard(request: Request) -> UrlGuard:
    guard: UrlGuard | None = getattr(request.app.state, "url_guard", None)
    if guard is None:  # pragma: no cover - defensive
        return UrlGuard.from_settings(get_settings())
    return guard


def get_answer_cache(request: Request) -> TTLCache[AnswerResponse] | None:
    """The process-wide `/v1/answer` cache, or ``None`` when it is disabled.

    Returned rather than injected into a long-lived service because `AnswerService`
    is built per request from the three clients it needs; only the cache has to
    survive across requests.
    """
    return getattr(request.app.state, "answer_cache", None)


def get_cost_ledger(request: Request) -> CostLedger:
    """The process-wide daily spend ledger (§6.4b)."""
    ledger: CostLedger | None = getattr(request.app.state, "cost_ledger", None)
    if ledger is None:  # pragma: no cover - defensive; startup always builds one
        return InMemoryCostLedger()
    return ledger


async def enforce_daily_cost_cap(
    request: Request,
    settings: Settings = Depends(get_settings),
    ledger: CostLedger = Depends(get_cost_ledger),
) -> None:
    """Refuse a spending endpoint once its caller is over budget for the day (§6.4b).

    Checked *before* any work: the point is to not spend, so rejecting after the
    LLM calls have already been paid for would be theatre. The check is coarse by
    construction — a request that starts under the cap is allowed to finish, so a
    key can overshoot by at most one run. Tightening that would mean estimating
    every run's cost up front, which is what `estimate_only` is for.
    """
    cap = settings.daily_cost_cap_usd
    if cap <= 0:
        return
    label = get_api_key_label(request)
    spent = await ledger.spent_today(label)
    if spent >= cap:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Daily cost cap reached for this API key: ${spent:.4f} spent of "
                f"${cap:.2f} allowed (UTC day). The budget resets at 00:00 UTC."
            ),
        )


async def record_spend(ledger: CostLedger, label: str, cost_usd: float) -> None:
    """Bill ``label`` for ``cost_usd``. Never raises — accounting is not the product."""
    if cost_usd <= 0:
        return
    try:
        await ledger.add(label, cost_usd)
    except Exception:  # noqa: BLE001 - a lost cent must not fail a served request
        logger.exception("Could not record spend for label %r", label)


async def record_usage(ledger: CostLedger, label: str, usage: UsageTracker) -> None:
    """Close the books on one run: bill the ledger, then export the metrics (§7.2).

    The single call every spending path makes when a run ends, successfully or
    not. The ledger only needs the cost (it enforces the daily cap); Prometheus
    also gets the token split and the pages the reranker saved, which are already
    sitting in the same tracker. Neither half raises.
    """
    await record_spend(ledger, label, usage.total_cost_usd)
    metrics.record_llm_usage(label, usage)


def get_jobs(request: Request) -> JobManager:
    jobs: JobManager | None = getattr(request.app.state, "jobs", None)
    if jobs is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="Job manager not initialized.")
    return jobs
