"""Single-pass, source-grounded answer endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_crawler, get_llm, get_searxng, require_api_key
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.search.searxng import SearxngClient, SearxngError
from app.models.answer import AnswerRequest, AnswerResponse
from app.services.answer import AnswerService

router = APIRouter(prefix="/v1", tags=["answer"], dependencies=[Depends(require_api_key)])


@router.post(
    "/answer",
    response_model=AnswerResponse,
    summary="Sourced answer (search + crawl + LLM)",
)
async def answer(
    req: AnswerRequest,
    searxng: SearxngClient = Depends(get_searxng),
    crawler: CrawlerService = Depends(get_crawler),
    llm: LLMClient = Depends(get_llm),
) -> AnswerResponse:
    service = AnswerService(searxng=searxng, crawler=crawler, llm=llm)
    try:
        return await service.answer(req)
    except SearxngError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # LLM / crawl failures
        raise HTTPException(status_code=502, detail=f"Answer generation failed: {exc}") from exc
