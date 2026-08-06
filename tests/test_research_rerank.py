"""Pre-crawl reranking: the three implementations, the fallback, and the cache."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.research.rerank import (
    CachedReranker,
    LexicalReranker,
    LLMReranker,
    NoopReranker,
    build_reranker,
)
from app.core.research.schemas import RankedResults
from app.models.search import SearchResultItem


def _item(title: str, url: str, snippet: str = "", **kwargs) -> SearchResultItem:
    return SearchResultItem(title=title, url=url, snippet=snippet, **kwargs)


RESULTS = [
    _item(
        "Aggregated links about batteries",
        "https://reddit.com/r/cars/comments/1",
        "Solid state battery discussion thread with many comments and links.",
    ),
    _item(
        "Solid state battery production ramp",
        "https://oem.example.com/solid-state",
        "Toyota confirms a solid state battery pilot line producing cells in 2027.",
    ),
    _item("Unrelated cooking blog", "https://food.example.com/soup", "Best onion soup recipes."),
]

GOAL = "solid state battery production timeline"


@pytest.mark.asyncio
async def test_noop_keeps_search_engine_order() -> None:
    ranked = await NoopReranker().rank(GOAL, RESULTS, 2)
    assert [r.url for r in ranked] == [RESULTS[0].url, RESULTS[1].url]


@pytest.mark.asyncio
async def test_lexical_ranks_the_on_topic_result_first() -> None:
    ranked = await LexicalReranker().rank(GOAL, RESULTS, 2)
    assert ranked[0].url == "https://oem.example.com/solid-state"
    assert len(ranked) == 2


@pytest.mark.asyncio
async def test_lexical_penalizes_aggregators_and_short_snippets() -> None:
    reranker = LexicalReranker()
    aggregator = _item("Solid state battery", "https://reddit.com/r/x", "solid state battery news")
    primary = _item("Solid state battery", "https://lab.example.org/x", "solid state battery news")
    scores = reranker.score(GOAL, [aggregator, primary])
    assert scores[1] > scores[0]

    verbose = _item("Battery report", "https://a.example.org/1", "solid state battery " * 5)
    terse = _item("Battery report", "https://b.example.org/1", "battery")
    scores = reranker.score(GOAL, [verbose, terse])
    assert scores[0] > scores[1]


@pytest.mark.asyncio
async def test_lexical_freshness_bonus_only_applies_with_a_time_range() -> None:
    reranker = LexicalReranker()
    now = datetime.now(UTC)
    recent = _item("Battery", "https://a.example.org/1", "battery", published_date=now.isoformat())
    old = _item(
        "Battery",
        "https://b.example.org/1",
        "battery",
        published_date=(now - timedelta(days=300)).isoformat(),
    )

    without_range = reranker.score("battery", [recent, old])
    assert without_range[0] == pytest.approx(without_range[1])  # dates ignored by default

    with_range = reranker.score("battery", [recent, old], time_range="year")
    assert with_range[0] > with_range[1]


class StubLLM:
    """Minimal stand-in for LLMClient.structured."""

    default_model = "test/model"

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0
        self.models: list[str | None] = []

    async def structured(self, messages, response_model, *, model=None, **kwargs):
        self.calls += 1
        self.models.append(model)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_llm_reranker_returns_the_kept_indices_in_order() -> None:
    llm = StubLLM(RankedResults(keep=[2, 1], dropped_reason={0: "link aggregator"}))
    ranked = await LLMReranker(llm, model="fast/model").rank(GOAL, RESULTS, 2)

    assert [r.url for r in ranked] == [RESULTS[2].url, RESULTS[1].url]
    assert llm.models == ["fast/model"]


@pytest.mark.asyncio
async def test_llm_reranker_falls_back_to_lexical_on_provider_failure() -> None:
    llm = StubLLM(error=TimeoutError("provider down"))
    ranked = await LLMReranker(llm).rank(GOAL, RESULTS, 2)

    # No exception escapes, and the lexical ordering takes over.
    expected = await LexicalReranker().rank(GOAL, RESULTS, 2)
    assert [r.url for r in ranked] == [r.url for r in expected]


@pytest.mark.asyncio
async def test_llm_reranker_falls_back_when_the_answer_is_unusable() -> None:
    # Out-of-range indices only: nothing usable, so the lexical ranker decides.
    llm = StubLLM(RankedResults(keep=[17, -3]))
    ranked = await LLMReranker(llm).rank(GOAL, RESULTS, 2)
    assert ranked[0].url == "https://oem.example.com/solid-state"


@pytest.mark.asyncio
async def test_llm_reranker_ignores_out_of_range_and_duplicate_indices() -> None:
    llm = StubLLM(RankedResults(keep=[1, 1, 99]))
    ranked = await LLMReranker(llm).rank(GOAL, RESULTS, 3)
    assert [r.url for r in ranked] == [RESULTS[1].url]


class CountingReranker:
    def __init__(self) -> None:
        self.calls = 0

    async def rank(self, goal, results, top_k, *, time_range=None, usage=None):
        self.calls += 1
        return results[:top_k]


@pytest.mark.asyncio
async def test_cached_reranker_pays_the_decision_once() -> None:
    inner = CountingReranker()
    cached = CachedReranker(inner, ttl=60.0)

    first = await cached.rank(GOAL, RESULTS, 2)
    second = await cached.rank(GOAL, RESULTS, 2)

    assert inner.calls == 1
    assert [r.url for r in first] == [r.url for r in second]


@pytest.mark.asyncio
async def test_cached_reranker_separates_goals_and_url_sets() -> None:
    inner = CountingReranker()
    cached = CachedReranker(inner, ttl=60.0)

    await cached.rank(GOAL, RESULTS, 2)
    await cached.rank("another goal entirely", RESULTS, 2)
    await cached.rank(GOAL, RESULTS[:2], 2)
    await cached.rank(GOAL, RESULTS, 3)

    assert inner.calls == 4


@pytest.mark.asyncio
async def test_cached_reranker_is_keyed_on_normalized_urls() -> None:
    inner = CountingReranker()
    cached = CachedReranker(inner, ttl=60.0)
    tracked = [_item(r.title, r.url + "?utm_source=serp", r.snippet) for r in RESULTS]

    await cached.rank(GOAL, RESULTS, 2)
    hit = await cached.rank(GOAL, tracked, 2)

    assert inner.calls == 1
    # Rebuilt from the live items, so the tracked URLs come back as given.
    assert [r.url for r in hit] == [tracked[0].url, tracked[1].url]


def test_build_reranker_maps_config_to_implementations() -> None:
    llm = StubLLM()
    assert isinstance(build_reranker("off"), NoopReranker)
    assert isinstance(build_reranker("lexical"), LexicalReranker)
    assert isinstance(build_reranker("llm", llm=llm, cache_ttl=60.0), CachedReranker)
    assert isinstance(build_reranker("llm", llm=llm, cache_ttl=0.0), LLMReranker)
    # An unknown mode degrades instead of blowing up at startup.
    assert isinstance(build_reranker("nonsense"), LexicalReranker)
