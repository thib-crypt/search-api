"""Crawl endpoint backed by crawl4ai."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_crawler, get_jobs, require_api_key
from app.core.crawl.crawler import CrawlerService
from app.jobs.manager import Emit, JobManager
from app.models.crawl import CrawlRequest, CrawlResponse, SiteCrawlRequest
from app.models.jobs import JobSubmitted

router = APIRouter(prefix="/v1", tags=["crawl"], dependencies=[Depends(require_api_key)])


@router.post("/crawl", response_model=CrawlResponse, summary="Crawl one or more URLs to Markdown")
async def crawl(
    req: CrawlRequest,
    crawler: CrawlerService = Depends(get_crawler),
) -> CrawlResponse:
    try:
        pages = await crawler.crawl(req)
    except Exception as exc:  # crawl4ai/browser failures
        raise HTTPException(status_code=502, detail=f"Crawl failed: {exc}") from exc
    return CrawlResponse(count=len(pages), results=pages)


@router.post(
    "/crawl/site",
    response_model=JobSubmitted,
    status_code=202,
    summary="Deep-crawl a whole site (async job)",
)
async def crawl_site(
    req: SiteCrawlRequest,
    crawler: CrawlerService = Depends(get_crawler),
    jobs: JobManager = Depends(get_jobs),
) -> JobSubmitted:
    job = jobs.create("site_crawl")

    async def task(emit: Emit) -> dict:
        pages = await crawler.crawl_site(req, emit=emit)
        return {
            "base_url": str(req.url),
            "count": len(pages),
            "pages": [p.model_dump() for p in pages],
        }

    jobs.submit(job, task)
    return JobSubmitted(
        job_id=job.id,
        type=job.type,
        status=job.status.value,
        stream_url=f"/v1/jobs/{job.id}/stream",
    )
