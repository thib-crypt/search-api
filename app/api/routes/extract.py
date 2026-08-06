"""Structured extraction endpoint (§5.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_crawler, get_llm, require_api_key
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.net.guard import BlockedUrlError
from app.models.extract import ExtractRequest, ExtractResponse
from app.services.extract import ExtractService

router = APIRouter(prefix="/v1", tags=["extract"], dependencies=[Depends(require_api_key)])


@router.post(
    "/extract",
    response_model=ExtractResponse,
    summary="Extract structured data from URLs (CSS selectors or LLM)",
)
async def extract(
    req: ExtractRequest,
    crawler: CrawlerService = Depends(get_crawler),
    llm: LLMClient = Depends(get_llm),
) -> ExtractResponse:
    service = ExtractService(crawler=crawler, llm=llm)
    try:
        results = await service.extract(req)
    except BlockedUrlError as exc:  # every URL refused by the network guard
        raise HTTPException(status_code=400, detail=f"URL not allowed: {exc}") from exc
    except ValueError as exc:  # unusable schema — the caller's input, not our fault
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Extraction failed: {exc}") from exc
    return ExtractResponse(count=len(results), results=results)
