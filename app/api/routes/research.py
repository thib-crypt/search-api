"""Deep-research endpoint.

Runs synchronously by default, or as an async job (``background: true``) whose
progress can be streamed via ``GET /v1/jobs/{id}/stream``. The clarification step
always runs synchronously first, so a client can answer before committing to a
long background run. The recursive engine lives in `app.core.research.engine`.

Three cost controls sit on this route (§6.4): a daily per-key cap checked before
any work starts, a per-run ceiling enforced inside the engine, and
``estimate_only`` which prices a run without launching it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import (
    enforce_daily_cost_cap,
    get_api_key_label,
    get_cost_ledger,
    get_crawler,
    get_jobs,
    get_llm,
    get_reranker,
    get_searxng,
    get_url_guard,
    record_usage,
    require_api_key,
)
from app.config import Settings, get_settings
from app.core.cost import CostLedger
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.llm.usage import UsageTracker
from app.core.net.guard import BlockedUrlError, UrlGuard
from app.core.net.webhook import deliver
from app.core.research.engine import (
    DeepResearchEngine,
    ResearchProgress,
    query_looks_specific,
)
from app.core.research.estimate import estimate_run
from app.core.research.rerank import Reranker
from app.core.search.searxng import SearxngClient, SearxngError
from app.jobs.manager import Emit, JobManager
from app.models.jobs import JobSubmitted
from app.models.research import ResearchEstimate, ResearchRequest, ResearchResponse
from app.models.usage import Usage

logger = logging.getLogger("unified_search_api.research")

router = APIRouter(prefix="/v1", tags=["research"], dependencies=[Depends(require_api_key)])


def _build_query_with_answers(query: str, answers: list[str]) -> str:
    if not answers:
        return query
    joined = "\n".join(f"- {a}" for a in answers)
    return f"{query}\n\nAdditional context from the user:\n{joined}"


async def _check_webhook(url: str, guard: UrlGuard, settings: Settings) -> None:
    """Validate a client-supplied callback target before a job is created.

    Refusing here rather than at delivery time means a typo or a blocked host is
    a 400 the client can act on, not a callback that silently never arrives. The
    URL is server-fetched and client-controlled, so it goes through the same SSRF
    guard as any crawl target (§2.1).
    """
    if not settings.webhook_secret:
        raise HTTPException(
            status_code=400,
            detail=(
                "webhook_url requires WEBHOOK_SECRET to be configured: an unsigned "
                "callback cannot be authenticated by its receiver."
            ),
        )
    try:
        await guard.check(url)
    except BlockedUrlError as exc:
        raise HTTPException(status_code=400, detail=f"webhook_url not allowed: {exc}") from exc


async def _notify(
    url: str | None, settings: Settings, payload: dict[str, Any], guard: UrlGuard
) -> None:
    """Fire the completion callback, if one was asked for. Never raises.

    A background run can take many minutes between `_check_webhook`'s guard
    check at submission and this call at completion — long enough for the
    target host's DNS to be repointed at a private address (the same
    rebinding window `CrawlerService._guard_landing` closes for crawled
    redirects). The guard is re-run here, at the moment the URL is actually
    fetched, not just once up front.
    """
    if not url:
        return
    try:
        await guard.check(url)
    except BlockedUrlError:
        logger.warning(
            "Webhook not sent: %s no longer passes the SSRF guard "
            "(re-checked at delivery time, job_id=%s)",
            url,
            payload.get("job_id"),
        )
        return
    await deliver(
        url, payload, secret=settings.webhook_secret, timeout_seconds=settings.webhook_timeout
    )


@router.post(
    "/research",
    summary="Deep research report (sync or async job)",
    dependencies=[Depends(enforce_daily_cost_cap)],
)
async def research(
    req: ResearchRequest,
    searxng: SearxngClient = Depends(get_searxng),
    crawler: CrawlerService = Depends(get_crawler),
    llm: LLMClient = Depends(get_llm),
    reranker: Reranker = Depends(get_reranker),
    jobs: JobManager = Depends(get_jobs),
    guard: UrlGuard = Depends(get_url_guard),
    ledger: CostLedger = Depends(get_cost_ledger),
    label: str = Depends(get_api_key_label),
    settings: Settings = Depends(get_settings),
) -> ResearchResponse | JobSubmitted:
    model_label = req.model or settings.report_model
    breadth = req.breadth or settings.research_default_breadth
    depth = req.depth or settings.research_default_depth

    # §6.4c: priced from the request alone, and returned before any client is
    # touched — no search, no crawl, no model call can happen on this path.
    if req.estimate_only:
        estimate = estimate_run(
            breadth=breadth,
            depth=depth,
            model=model_label or settings.llm_model,
            crawl_top_k=settings.research_crawl_top_k,
            process_context_token_budget=settings.research_process_context_token_budget,
            report_token_budget=settings.research_report_token_budget,
            rerank_enabled=settings.research_rerank == "llm",
        )
        return ResearchResponse(
            status="estimated",
            query=req.query,
            model=model_label,
            estimate=ResearchEstimate(**estimate),
        )

    if req.webhook_url:
        await _check_webhook(req.webhook_url, guard, settings)

    engine = DeepResearchEngine(
        searxng=searxng,
        crawler=crawler,
        llm=llm,
        reranker=reranker,
        concurrency=settings.research_concurrency,
        report_model=settings.report_model,
        report_token_budget=settings.research_report_token_budget,
        report_max_tokens=settings.llm_report_max_tokens,
        results_per_query=settings.research_results_per_query,
        crawl_top_k=settings.research_crawl_top_k,
        per_page_token_budget=settings.research_per_page_token_budget,
        process_context_token_budget=settings.research_process_context_token_budget,
        num_learnings=settings.research_num_learnings,
        num_followups=settings.research_num_followups,
        max_cost_usd=settings.research_max_cost_usd,
    )
    answers = req.answers or []
    # One tracker for this whole request, owned here rather than by the engine so
    # a run that raises halfway through still bills what it already spent.
    usage = UsageTracker()

    # Clarification step (tri-state). Skipped when interactive is False or when the
    # user already supplied answers. Always synchronous.
    #
    # §4.4: in "auto" mode a specific-looking query skips the LLM round-trip
    # entirely (heuristic, not a model call). Chosen over merging clarification
    # and SERP-query generation into one structured call because it keeps the
    # route's early-return shape intact and costs nothing when it fires; a query
    # the heuristic is unsure about still goes to the model.
    skip_by_heuristic = req.interactive == "auto" and query_looks_specific(req.query)
    if req.interactive is not False and not answers and not skip_by_heuristic:
        try:
            questions = await engine.generate_feedback(
                req.query, model=req.model, force=req.interactive is True, usage=usage
            )
        except Exception as exc:
            await record_usage(ledger, label, usage)
            raise HTTPException(status_code=502, detail=f"Feedback step failed: {exc}") from exc
        if questions:
            await record_usage(ledger, label, usage)
            return ResearchResponse(
                status="needs_clarification",
                query=req.query,
                model=model_label,
                questions=questions,
                usage=Usage.from_tracker(usage),
            )

    effective_query = _build_query_with_answers(req.query, answers)

    # `stream=true` implies a job: `GET /v1/jobs/{id}/stream` is the app's only SSE
    # channel, and a synchronous response cannot carry tokens as they are produced.
    if req.background or req.stream:
        job = await jobs.create("research")

        async def task(emit: Emit) -> dict[str, Any]:
            async def on_progress(p: ResearchProgress) -> None:
                await emit(
                    {
                        "stage": p.stage,
                        "depth": f"{p.current_depth}/{p.total_depth}",
                        "queries": f"{p.completed_queries}/{p.total_queries}",
                        "message": p.message,
                    }
                )

            async def on_token(token: str) -> None:
                # JobManager wraps every emit as {"event": "progress", ...}, so the
                # discriminator lives in `stage` — the same field the coarse
                # progress events already use — rather than a second "event" key.
                await emit({"stage": "report_token", "token": token})

            try:
                result = await engine.run(
                    effective_query,
                    breadth=breadth,
                    depth=depth,
                    model=req.model,
                    on_progress=on_progress,
                    on_token=on_token if req.stream else None,
                    time_range=req.time_range,
                    include_domains=req.include_domains,
                    exclude_domains=req.exclude_domains,
                    usage=usage,
                )
            except Exception as exc:
                # Bill what was spent before the failure, then tell the webhook:
                # a client waiting on a callback needs to hear about failures too,
                # or it waits forever for a run that is never coming.
                await record_usage(ledger, label, usage)
                await _notify(
                    req.webhook_url,
                    settings,
                    {
                        "job_id": job.id,
                        "type": job.type,
                        "status": "failed",
                        "error": str(exc),
                        "usage": usage.snapshot(),
                    },
                    guard,
                )
                raise

            await record_usage(ledger, label, usage)
            payload = {
                "query": req.query,
                "model": model_label,
                "report": result.report,
                "learnings": [item.text for item in result.learnings],
                "sources": result.sources,
                "citations": [c.model_dump() for c in result.citations],
                "serp_queries": result.serp_queries,
                "stopped_early": result.stopped_early,
                "usage": result.usage.snapshot(),
            }
            await _notify(
                req.webhook_url,
                settings,
                {"job_id": job.id, "type": job.type, "status": "completed", "result": payload},
                guard,
            )
            return payload

        jobs.submit(job, task)
        return JobSubmitted(
            job_id=job.id,
            type=job.type,
            status=job.status.value,
            stream_url=f"/v1/jobs/{job.id}/stream",
        )

    try:
        coro = engine.run(
            effective_query,
            breadth=breadth,
            depth=depth,
            model=req.model,
            time_range=req.time_range,
            include_domains=req.include_domains,
            exclude_domains=req.exclude_domains,
            usage=usage,
        )
        if settings.research_sync_timeout > 0:
            result = await asyncio.wait_for(coro, timeout=settings.research_sync_timeout)
        else:
            result = await coro
    except TimeoutError:
        await record_usage(ledger, label, usage)
        raise HTTPException(
            status_code=504,
            detail=(
                f"Research timed out after {settings.research_sync_timeout:.0f}s. "
                "Use background=true for long queries."
            ),
        ) from None
    except SearxngError as exc:
        await record_usage(ledger, label, usage)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        await record_usage(ledger, label, usage)
        raise HTTPException(status_code=502, detail=f"Research failed: {exc}") from exc

    await record_usage(ledger, label, result.usage)
    return ResearchResponse(
        status="completed",
        query=req.query,
        model=model_label,
        report=result.report,
        learnings=[item.text for item in result.learnings],
        sources=result.sources,
        citations=result.citations,
        serp_queries=result.serp_queries,
        usage=Usage.from_tracker(result.usage),
        stopped_early=result.stopped_early,
    )
