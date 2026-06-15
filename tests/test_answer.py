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
