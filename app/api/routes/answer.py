"""Single-pass, source-grounded answer endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import ORJSONResponse
from sse_starlette.sse import EventSourceResponse

from app.api.deps import (
    enforce_daily_cost_cap,
    get_answer_cache,
    get_api_key_label,
    get_cost_ledger,
    get_crawler,
    get_llm,
    get_searxng,
    record_usage,
    require_api_key,
)
from app.core.cache import TTLCache
from app.core.cost import CostLedger
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.llm.usage import UsageTracker
from app.core.search.searxng import SearxngClient, SearxngError
from app.models.answer import AnswerRequest, AnswerResponse
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
    # §6.4b: this endpoint spends provider credits, so it is behind the daily cap.
    dependencies=[Depends(enforce_daily_cost_cap)],
)
async def answer(
    req: AnswerRequest,
    searxng: SearxngClient = Depends(get_searxng),
    crawler: CrawlerService = Depends(get_crawler),
    llm: LLMClient = Depends(get_llm),
    cache: TTLCache[AnswerResponse] | None = Depends(get_answer_cache),
    ledger: CostLedger = Depends(get_cost_ledger),
    label: str = Depends(get_api_key_label),
    # `response_model=None` above, so this union is a type annotation only: FastAPI
    # does not try to build a schema from it.
) -> AnswerResponse | EventSourceResponse:
    service = AnswerService(searxng=searxng, crawler=crawler, llm=llm, cache=cache)
    # §6.5: one tracker per call, surfaced in the response and billed to the key.
    usage = UsageTracker()

    if req.stream:

        async def event_source() -> AsyncIterator[dict[str, Any]]:
            try:
                async for event in service.stream_answer(req, usage):
                    yield event
            except SearxngError as exc:
                import json

                yield {"event": "error", "data": json.dumps({"detail": str(exc)})}
            except Exception as exc:
                import json

                yield {"event": "error", "data": json.dumps({"detail": f"Answer failed: {exc}"})}
            finally:
                # In a `finally` so a disconnected client still gets billed for
                # the tokens the provider already produced.
                await record_usage(ledger, label, usage)

        return EventSourceResponse(event_source())

    try:
        response = await service.answer(req, usage)
    except SearxngError as exc:
        await record_usage(ledger, label, usage)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        await record_usage(ledger, label, usage)
        raise HTTPException(status_code=502, detail=f"Answer generation failed: {exc}") from exc
    await record_usage(ledger, label, usage)
    return response
