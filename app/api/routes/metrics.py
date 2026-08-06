"""Prometheus scrape endpoint (§7.2).

Behind the same API key as every other endpoint. The plan allows either auth or
network restriction; this app already has an auth mechanism, and the payload is
not innocuous — ``llm_cost_usd_total`` and ``llm_tokens_total`` are labelled per
API key, so an open ``/metrics`` publishes what each of your clients spends. An
operator who prefers anonymous scraping restricts the route at the reverse proxy
and gives the scraper a key, rather than this route growing a second gate.

Exempt from rate limiting (see ``_EXEMPT_PATHS`` in `app.api.middleware`): a
scrape runs on its own schedule and must not draw from a caller's budget.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import require_api_key
from app.core import metrics

router = APIRouter(tags=["meta"], dependencies=[Depends(require_api_key)])


@router.get(
    "/metrics",
    summary="Prometheus metrics (requires an API key)",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
async def prometheus_metrics(request: Request) -> Response:
    # Gauges that are cheaper to compute than to maintain are sampled here, at
    # scrape time: a count derived from the live job table cannot drift the way
    # an incremented counter can.
    jobs = getattr(request.app.state, "jobs", None)
    if jobs is not None:
        metrics.set_jobs_active(jobs.active_counts())
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
