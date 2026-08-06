"""Single-pass, source-grounded answer endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import ORJSONResponse
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_crawler, get_llm, get_searxng, require_api_key
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.search.searxng import SearxngClient, SearxngError
from app.models.answer import AnswerRequest
from app.services.answer import AnswerService

router = APIRouter(prefix="/v1", tags=["answer"], dependencies=[Depends(require_api_key)])


@router.post(
    "/answer",
    response_model=None,
    # The union with EventSourceResponse forces response_model=None, which rules out
    # FastAPI's serialize-straight-to-bytes fast path, so this is the one route where
    # orjson is a real win over the stdlib encoder (§3.3). Applied per-route rather
    # than app-wide: an app-wide default would disable the fast path everywhere else.
    # Only the non-streaming branch uses it -- returning EventSourceResponse directly
    # bypasses response_class entirely.
    response_class=ORJSONResponse,
    summary="Sourced answer (search + crawl + LLM). Set stream=true for Server-Sent Events.",
)
async def answer(
    req: AnswerRequest,
    searxng: SearxngClient = Depends(get_searxng),
    crawler: CrawlerService = Depends(get_crawler),
    llm: LLMClient = Depends(get_llm),
):
    service = AnswerService(searxng=searxng, crawler=crawler, llm=llm)

    if req.stream:
        async def event_source():
            try:
                async for event in service.stream_answer(req):
                    yield event
            except SearxngError as exc:
                import json
                yield {"event": "error", "data": json.dumps({"detail": str(exc)})}
            except Exception as exc:
                import json
                yield {"event": "error", "data": json.dumps({"detail": f"Answer failed: {exc}"})}

        return EventSourceResponse(event_source())

    try:
        return await service.answer(req)
    except SearxngError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Answer generation failed: {exc}") from exc
