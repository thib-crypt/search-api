"""Deep-research endpoint.

Runs synchronously by default, or as an async job (``background: true``) whose
progress can be streamed via ``GET /v1/jobs/{id}/stream``. The clarification step
always runs synchronously first, so a client can answer before committing to a
long background run. The recursive engine lives in `app.core.research.engine`.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_crawler, get_jobs, get_llm, get_searxng, require_api_key
from app.config import Settings, get_settings
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.research.engine import DeepResearchEngine, ResearchProgress
from app.core.search.searxng import SearxngClient, SearxngError
from app.jobs.manager import Emit, JobManager
from app.models.jobs import JobSubmitted
from app.models.research import ResearchRequest, ResearchResponse

router = APIRouter(prefix="/v1", tags=["research"], dependencies=[Depends(require_api_key)])


def _build_query_with_answers(query: str, answers: list[str]) -> str:
    if not answers:
        return query
    joined = "\n".join(f"- {a}" for a in answers)
    return f"{query}\n\nAdditional context from the user:\n{joined}"


@router.post("/research", summary="Deep research report (sync or async job)")
async def research(
    req: ResearchRequest,
    searxng: SearxngClient = Depends(get_searxng),
    crawler: CrawlerService = Depends(get_crawler),
    llm: LLMClient = Depends(get_llm),
    jobs: JobManager = Depends(get_jobs),
    settings: Settings = Depends(get_settings),
) -> ResearchResponse | JobSubmitted:
    engine = DeepResearchEngine(
        searxng=searxng,
        crawler=crawler,
        llm=llm,
        concurrency=settings.research_concurrency,
        report_model=settings.report_model,
    )
    model_label = req.model or settings.report_model
    answers = req.answers or []

    # Clarification step (tri-state). Skipped when interactive is False or when the
    # user already supplied answers. Always synchronous.
    if req.interactive is not False and not answers:
        try:
            questions = await engine.generate_feedback(
                req.query, model=req.model, force=req.interactive is True
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Feedback step failed: {exc}") from exc
        if questions:
            return ResearchResponse(
                status="needs_clarification",
                query=req.query,
                model=model_label,
                questions=questions,
            )

    breadth = req.breadth or settings.research_default_breadth
    depth = req.depth or settings.research_default_depth
    effective_query = _build_query_with_answers(req.query, answers)

    if req.background:
        job = jobs.create("research")

        async def task(emit: Emit) -> dict:
            async def on_progress(p: ResearchProgress) -> None:
                await emit(
                    {
                        "stage": p.stage,
                        "depth": f"{p.current_depth}/{p.total_depth}",
                        "queries": f"{p.completed_queries}/{p.total_queries}",
                        "message": p.message,
                    }
                )

            result = await engine.run(
                effective_query,
                breadth=breadth,
                depth=depth,
                model=req.model,
                on_progress=on_progress,
            )
            return {
                "query": req.query,
                "model": model_label,
                "report": result.report,
                "learnings": result.learnings,
                "sources": result.sources,
                "serp_queries": result.serp_queries,
            }

        jobs.submit(job, task)
        return JobSubmitted(
            job_id=job.id,
            type=job.type,
            status=job.status.value,
            stream_url=f"/v1/jobs/{job.id}/stream",
        )

    try:
        coro = engine.run(effective_query, breadth=breadth, depth=depth, model=req.model)
        if settings.research_sync_timeout > 0:
            result = await asyncio.wait_for(coro, timeout=settings.research_sync_timeout)
        else:
            result = await coro
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Research timed out after {settings.research_sync_timeout:.0f}s. "
                "Use background=true for long queries."
            ),
        ) from None
    except SearxngError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Research failed: {exc}") from exc

    return ResearchResponse(
        status="completed",
        query=req.query,
        model=model_label,
        report=result.report,
        learnings=result.learnings,
        sources=result.sources,
        serp_queries=result.serp_queries,
    )
