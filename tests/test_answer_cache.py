"""/v1/answer response cache (§5.7)."""

from __future__ import annotations

from app.core.cache import TTLCache
from app.models.answer import AnswerRequest, AnswerResponse
from app.models.crawl import CrawledPage
from app.models.search import SearchResponse, SearchResultItem
from app.services.answer import AnswerService


class CountingSearxng:
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, req) -> SearchResponse:
        self.calls += 1
        return SearchResponse(
            query=req.q,
            number_of_results=1,
            results=[SearchResultItem(title="T", url="https://a.example/", snippet="s")],
        )


class CountingCrawler:
    def __init__(self) -> None:
        self.calls = 0

    async def crawl(self, req) -> list[CrawledPage]:
        self.calls += 1
        return [CrawledPage(url=str(u), success=True, markdown="body") for u in req.urls]


class CountingLLM:
    default_model = "test/model"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, **kwargs) -> str:
        self.calls += 1
        return f"answer #{self.calls}"


def _service(cache: TTLCache | None):
    searxng, crawler, llm = CountingSearxng(), CountingCrawler(), CountingLLM()
    return AnswerService(searxng=searxng, crawler=crawler, llm=llm, cache=cache), searxng, llm


async def test_second_identical_call_hits_the_cache() -> None:
    cache: TTLCache[AnswerResponse] = TTLCache(ttl=60, max_size=8)
    service, searxng, llm = _service(cache)
    req = AnswerRequest(query="what is x?")

    first = await service.answer(req)
    second = await service.answer(req)

    assert first.answer == second.answer == "answer #1"
    assert llm.calls == 1  # the model was not asked twice
    assert searxng.calls == 1  # nor was SearXNG


async def test_cache_key_covers_query_model_max_sources_and_language() -> None:
    cache: TTLCache[AnswerResponse] = TTLCache(ttl=60, max_size=8)
    service, _, llm = _service(cache)

    await service.answer(AnswerRequest(query="q"))
    await service.answer(AnswerRequest(query="other"))
    await service.answer(AnswerRequest(query="q", model="other/model"))
    await service.answer(AnswerRequest(query="q", max_sources=7))
    await service.answer(AnswerRequest(query="q", language="fr"))

    assert llm.calls == 5  # every varied field is part of the key


async def test_fields_outside_the_key_share_an_entry() -> None:
    """`fetch_full` is deliberately not in the §5.7 key tuple."""
    cache: TTLCache[AnswerResponse] = TTLCache(ttl=60, max_size=8)
    service, _, llm = _service(cache)

    await service.answer(AnswerRequest(query="q", fetch_full=True))
    await service.answer(AnswerRequest(query="q", fetch_full=False))

    assert llm.calls == 1


async def test_cached_response_cannot_be_mutated_by_a_caller() -> None:
    cache: TTLCache[AnswerResponse] = TTLCache(ttl=60, max_size=8)
    service, _, _ = _service(cache)

    first = await service.answer(AnswerRequest(query="q"))
    first.answer = "tampered"
    first.sources.clear()

    second = await service.answer(AnswerRequest(query="q"))
    assert second.answer == "answer #1"
    assert second.sources  # the cached entry kept its own list


async def test_no_cache_means_every_call_runs_the_pipeline() -> None:
    service, searxng, llm = _service(None)

    await service.answer(AnswerRequest(query="q"))
    await service.answer(AnswerRequest(query="q"))

    assert llm.calls == 2
    assert searxng.calls == 2


async def test_streaming_neither_reads_nor_writes_the_cache() -> None:
    cache: TTLCache[AnswerResponse] = TTLCache(ttl=60, max_size=8)
    searxng, crawler, llm = CountingSearxng(), CountingCrawler(), CountingLLM()

    async def stream_complete(messages, **kwargs):
        for token in ("a", "b"):
            yield token

    llm.stream_complete = stream_complete
    service = AnswerService(searxng=searxng, crawler=crawler, llm=llm, cache=cache)

    events = [e async for e in service.stream_answer(AnswerRequest(query="q", stream=True))]
    assert any(e["event"] == "token" for e in events)

    # The stream left no entry behind, so the blocking call still does the work.
    await service.answer(AnswerRequest(query="q"))
    assert llm.calls == 1
