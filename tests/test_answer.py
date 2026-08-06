import asyncio
import json

import pytest
from starlette.testclient import TestClient

from app.api.deps import get_crawler, get_llm, get_searxng
from app.main import app
from app.models.answer import AnswerRequest
from app.models.crawl import CrawledPage
from app.models.search import SearchResponse, SearchResultItem
from app.services.answer import AnswerService


class FakeSearxng:
    async def search(self, req) -> SearchResponse:
        items = [
            SearchResultItem(title="First", url="https://a.com", snippet="snippet a"),
            SearchResultItem(title="Second", url="https://b.com", snippet="snippet b"),
        ]
        return SearchResponse(
            query=req.q, number_of_results=len(items), results=items[: req.max_results or 2]
        )


class FakeCrawler:
    def __init__(self) -> None:
        self.called = False

    async def crawl(self, req) -> list[CrawledPage]:
        self.called = True
        return [
            CrawledPage(url=str(u), success=True, status_code=200, markdown=f"full text of {u}")
            for u in req.urls
        ]


class FakeLLM:
    default_model = "test/model"

    def __init__(self) -> None:
        self.last_messages = None

    async def complete(self, messages, *, model=None, **kwargs) -> str:
        self.last_messages = messages
        return "Synthesized answer citing [1] and [2]."

    async def stream_complete(self, messages, *, model=None, **kwargs):
        self.last_messages = messages
        for token in ["Streamed ", "answer."]:
            yield token


def _override(searxng, crawler, llm):
    app.dependency_overrides[get_searxng] = lambda: searxng
    app.dependency_overrides[get_crawler] = lambda: crawler
    app.dependency_overrides[get_llm] = lambda: llm


def test_answer_endpoint_full_pipeline() -> None:
    crawler = FakeCrawler()
    _override(FakeSearxng(), crawler, FakeLLM())
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/answer", json={"query": "what is x?", "max_sources": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"].startswith("Synthesized answer")
        assert body["model"] == "test/model"
        assert len(body["sources"]) == 2
        assert body["sources"][0]["index"] == 1
        assert body["sources"][0]["used_full_text"] is True
        assert crawler.called is True
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_stream_answer_yields_searching_tokens_done() -> None:
    service = AnswerService(searxng=FakeSearxng(), crawler=FakeCrawler(), llm=FakeLLM())
    events = []
    async for ev in service.stream_answer(AnswerRequest(query="q", stream=True)):
        events.append(ev)

    event_types = [e["event"] for e in events]
    assert event_types[0] == "searching"
    assert "token" in event_types
    assert event_types[-1] == "done"


@pytest.mark.asyncio
async def test_stream_emits_crawling_before_it_blocks_on_the_crawl() -> None:
    """The crawl event is a progress signal: it has to precede the crawl, not report it."""

    class SlowCrawler(FakeCrawler):
        def __init__(self) -> None:
            super().__init__()
            self.finished = False

        async def crawl(self, req):
            await asyncio.sleep(0.05)
            pages = await super().crawl(req)
            self.finished = True
            return pages

    crawler = SlowCrawler()
    service = AnswerService(searxng=FakeSearxng(), crawler=crawler, llm=FakeLLM())
    events = []
    async for ev in service.stream_answer(AnswerRequest(query="q", stream=True)):
        if ev["event"] == "crawling":
            assert crawler.finished is False  # announced while it runs, not after
        events.append(ev)

    assert [e["event"] for e in events][:3] == ["searching", "crawling", "generating"]
    assert events[-1]["event"] == "done"


@pytest.mark.asyncio
async def test_answer_and_stream_build_the_same_context() -> None:
    """`_prepare_context` is shared: the two entry points must feed the model alike."""
    sync_llm = FakeLLM()
    stream_llm = FakeLLM()
    req = AnswerRequest(query="what is x?", max_sources=2)

    sync_resp = await AnswerService(FakeSearxng(), FakeCrawler(), sync_llm).answer(req)
    stream_service = AnswerService(FakeSearxng(), FakeCrawler(), stream_llm)
    stream_events = [ev async for ev in stream_service.stream_answer(req)]

    assert sync_llm.last_messages == stream_llm.last_messages
    done = json.loads(stream_events[-1]["data"])
    assert done["sources"] == [s.model_dump() for s in sync_resp.sources]


@pytest.mark.asyncio
async def test_service_skips_crawl_when_fetch_full_false() -> None:
    crawler = FakeCrawler()
    llm = FakeLLM()
    service = AnswerService(searxng=FakeSearxng(), crawler=crawler, llm=llm)
    resp = await service.answer(AnswerRequest(query="q", max_sources=2, fetch_full=False))

    assert crawler.called is False
    assert all(not s.used_full_text for s in resp.sources)
    # The snippet content must have made it into the prompt context.
    user_msg = llm.last_messages[-1]["content"]
    assert "snippet a" in user_msg
