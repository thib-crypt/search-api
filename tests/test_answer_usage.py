"""`/v1/answer` reports what it cost (§6.5).

A per-call figure is as much a product feature as a metric: without it there is
no way to know what a question costs before asking a thousand of them.
"""

from __future__ import annotations

import json

from starlette.testclient import TestClient

from app.api.deps import get_crawler, get_llm, get_searxng
from app.core.llm.usage import UsageTracker
from app.main import app
from app.models.answer import AnswerRequest
from app.services.answer import AnswerService
from tests.test_answer import FakeCrawler, FakeLLM, FakeSearxng


class BilledLLM(FakeLLM):
    """Records into the tracker it is handed, as the real client does."""

    async def complete(self, messages, *, model=None, usage=None, **kwargs) -> str:
        if usage is not None:
            usage.prompt_tokens += 1200
            usage.completion_tokens += 80
            usage.total_cost_usd += 0.0004
        return await super().complete(messages, model=model, **kwargs)

    async def stream_complete(self, messages, *, model=None, usage=None, **kwargs):
        async for token in super().stream_complete(messages, model=model, **kwargs):
            yield token
        # Providers report usage on the *last* chunk, after the text is out.
        if usage is not None:
            usage.prompt_tokens += 900
            usage.total_cost_usd += 0.0002


def test_the_response_carries_its_usage() -> None:
    app.dependency_overrides[get_searxng] = lambda: FakeSearxng()
    app.dependency_overrides[get_crawler] = lambda: FakeCrawler()
    app.dependency_overrides[get_llm] = lambda: BilledLLM()
    try:
        with TestClient(app) as client:
            body = client.post(
                "/v1/answer", json={"query": "unique question one", "max_sources": 2}
            ).json()
    finally:
        app.dependency_overrides.clear()

    usage = body["usage"]
    assert usage["prompt_tokens"] == 1200
    assert usage["completion_tokens"] == 80
    assert usage["total_cost_usd"] > 0
    assert usage["searches"] == 1
    assert usage["pages_crawled"] == 2


async def test_a_cache_hit_reports_no_spend() -> None:
    """Replaying a stored answer costs nothing, so it must not be billed twice."""
    from app.core.cache import TTLCache

    cache = TTLCache(ttl=60.0, max_size=8)
    service = AnswerService(
        searxng=FakeSearxng(), crawler=FakeCrawler(), llm=BilledLLM(), cache=cache
    )
    req = AnswerRequest(query="cached question")

    first = await service.answer(req, UsageTracker())
    second = await service.answer(req, UsageTracker())

    assert first.usage.total_cost_usd > 0
    assert second.answer == first.answer
    assert second.usage.total_cost_usd == 0.0
    assert second.usage.prompt_tokens == 0


async def test_the_final_sse_event_carries_the_usage() -> None:
    service = AnswerService(searxng=FakeSearxng(), crawler=FakeCrawler(), llm=BilledLLM())
    usage = UsageTracker()

    events = [event async for event in service.stream_answer(AnswerRequest(query="q"), usage)]

    assert events[-1]["event"] == "done"
    reported = json.loads(events[-1]["data"])["usage"]
    # Emitted after the stream closes, which is the only point at which the
    # provider's usage chunk has been seen.
    assert reported["prompt_tokens"] == 900
    assert reported["total_cost_usd"] > 0
    assert reported["searches"] == 1


async def test_usage_is_optional_everywhere() -> None:
    """Nothing breaks when no tracker is threaded through (the engine's own tests)."""
    service = AnswerService(searxng=FakeSearxng(), crawler=FakeCrawler(), llm=FakeLLM())
    response = await service.answer(AnswerRequest(query="no tracker"))
    assert response.usage.total_cost_usd == 0.0
