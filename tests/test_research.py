import asyncio

import pytest
from starlette.testclient import TestClient

from app.api.deps import get_crawler, get_llm, get_reranker, get_searxng
from app.core.research.engine import DeepResearchEngine, ResearchProgress, query_looks_specific
from app.core.research.rerank import NoopReranker
from app.core.research.schemas import (
    FeedbackQuestions,
    LearningDraft,
    SerpProcessing,
    SerpQuery,
    SerpQueryList,
)
from app.main import app
from app.models.crawl import CrawledPage
from app.models.search import SearchResponse, SearchResultItem


class FakeSearxng:
    """Two results per query, distinct per query.

    URLs embed the query on purpose: the engine now dedupes URLs run-wide, so a
    fake returning the same two links for every query would starve every branch
    but the first. Sharing is exercised explicitly in `test_research_dedup.py`.
    """

    async def search(self, req) -> SearchResponse:
        slug = req.q.replace(" ", "-")[:24]
        items = [
            SearchResultItem(title=f"A {req.q}", url=f"https://a.com/{slug}", snippet="sa"),
            SearchResultItem(title=f"B {req.q}", url=f"https://b.com/{slug}", snippet="sb"),
        ]
        return SearchResponse(query=req.q, number_of_results=2, results=items)


class FakeCrawler:
    async def crawl(self, req) -> list[CrawledPage]:
        return [
            CrawledPage(url=str(u), success=True, status_code=200, markdown=f"content {u}")
            for u in req.urls
        ]


class FakeLLM:
    default_model = "test/model"

    def __init__(self, feedback_questions=None) -> None:
        self.feedback_questions = feedback_questions or []
        self.serp_calls = 0
        self.report_messages = None

    async def structured(self, messages, response_model, *, model=None, **kwargs):
        name = response_model.__name__
        if name == "FeedbackQuestions":
            return FeedbackQuestions(questions=self.feedback_questions)
        if name == "SerpQueryList":
            self.serp_calls += 1
            n = self.serp_calls
            return SerpQueryList(
                queries=[
                    SerpQuery(query=f"q{n}a", research_goal="goal"),
                    SerpQuery(query=f"q{n}b", research_goal="goal"),
                ]
            )
        if name == "SerpProcessing":
            return SerpProcessing(
                learnings=[
                    LearningDraft(text="learning X", source_indices=[1]),
                    LearningDraft(text="learning Y", source_indices=[2]),
                ],
                follow_up_questions=["next?"],
            )
        raise AssertionError(f"unexpected response_model {name}")

    async def complete(self, messages, **kwargs) -> str:
        # The final report is a free-text completion now, not a structured call.
        self.report_messages = messages
        return "# Report\n\nDetailed body citing [1]."


def _engine(llm=None, concurrency=2):
    return DeepResearchEngine(
        searxng=FakeSearxng(), crawler=FakeCrawler(), llm=llm or FakeLLM(), concurrency=concurrency
    )


@pytest.mark.asyncio
async def test_run_depth_one_aggregates_and_appends_sources() -> None:
    engine = _engine()
    result = await engine.run("topic", breadth=2, depth=1)

    # Deduped across branches, with the sources of both merged onto one learning.
    assert [item.text for item in result.learnings] == ["learning X", "learning Y"]
    assert result.learnings[0].source_urls == ["https://a.com/q1a", "https://a.com/q1b"]
    assert "## Sources" in result.report
    assert result.report.startswith("# Report")
    assert set(result.sources) == {
        "https://a.com/q1a",
        "https://b.com/q1a",
        "https://a.com/q1b",
        "https://b.com/q1b",
    }
    assert result.serp_queries == ["q1a", "q1b"]


@pytest.mark.asyncio
async def test_report_carries_inline_citations() -> None:
    llm = FakeLLM()
    result = await _engine(llm).run("topic", breadth=2, depth=1)

    # Every source is numbered, and the numbering is what the report is told to cite.
    assert [c.index for c in result.citations] == [1, 2, 3, 4]
    assert [c.url for c in result.citations] == result.sources
    prompt = llm.report_messages[-1]["content"]
    assert "learning X [1]" in prompt  # learnings reach the model already attributed
    assert "[1] https://a.com/q1a" in prompt
    assert "Cite them inline" in prompt
    assert "[1] https://a.com/q1a" in result.report  # numbered appendix


@pytest.mark.asyncio
async def test_run_depth_two_recurses() -> None:
    llm = FakeLLM()
    engine = DeepResearchEngine(
        searxng=FakeSearxng(), crawler=FakeCrawler(), llm=llm, concurrency=1
    )
    result = await engine.run("topic", breadth=2, depth=2)

    # Top level generates 2 queries; each branch recurses with breadth ceil(2/2)=1,
    # so one extra query per branch -> 3 generate calls, 4 queries total.
    assert llm.serp_calls == 3
    assert len(result.serp_queries) == 4


class CountingCrawler:
    """Tracks how many crawls the engine has in flight at once."""

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.in_flight = 0
        self.peak = 0

    async def crawl(self, req) -> list[CrawledPage]:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            return [
                CrawledPage(url=str(u), success=True, status_code=200, markdown=f"c {u}")
                for u in req.urls
            ]
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_concurrency_is_capped_across_recursion_levels() -> None:
    """The semaphore used to be recreated per level, so real concurrency was
    `concurrency ** depth` — 8 simultaneous branches at concurrency=2, depth=3."""
    crawler = CountingCrawler()
    engine = DeepResearchEngine(
        searxng=FakeSearxng(), crawler=crawler, llm=FakeLLM(), concurrency=2
    )

    await engine.run("topic", breadth=4, depth=3)

    assert crawler.peak <= 2


@pytest.mark.asyncio
async def test_learnings_are_trimmed_to_the_report_token_budget() -> None:
    """An overflowing report prompt would waste the whole run at the last step."""
    captured: list[list[str]] = []

    class CapturingLLM(FakeLLM):
        async def structured(self, messages, response_model, *, model=None, **kwargs):
            if response_model.__name__ == "SerpProcessing":
                return SerpProcessing(
                    learnings=[
                        LearningDraft(text=f"learning {i} " + "word " * 50, source_indices=[1])
                        for i in range(20)
                    ],
                    follow_up_questions=[],
                )
            return await super().structured(messages, response_model, model=model, **kwargs)

        async def complete(self, messages, **kwargs) -> str:
            captured.append(messages[-1]["content"].split("\n"))
            return await super().complete(messages, **kwargs)

    engine = DeepResearchEngine(
        searxng=FakeSearxng(),
        crawler=FakeCrawler(),
        llm=CapturingLLM(),
        concurrency=2,
        report_token_budget=200,
    )
    result = await engine.run("topic", breadth=2, depth=1)

    prompt = "\n".join(captured[0])
    assert "learning 0" in prompt
    assert "learning 19" not in prompt  # dropped: the budget ran out first
    assert len(result.learnings) == 20  # the response still reports everything found


class WideSearxng:
    """Records the SERP width asked for and answers with that many results."""

    def __init__(self) -> None:
        self.max_results: list[int | None] = []

    async def search(self, req) -> SearchResponse:
        self.max_results.append(req.max_results)
        items = [
            SearchResultItem(title=f"T{i}", url=f"https://s{i}.com/{req.q}", snippet=f"sn{i}")
            for i in range(req.max_results or 10)
        ]
        return SearchResponse(query=req.q, number_of_results=len(items), results=items)


@pytest.mark.asyncio
async def test_serp_is_widened_but_only_the_reranked_top_k_is_crawled() -> None:
    """Titles and snippets are free; crawling is not. Ask for more, fetch less."""
    searxng = WideSearxng()
    crawled: list[str] = []

    class TrackingCrawler(FakeCrawler):
        async def crawl(self, req):
            crawled.extend(str(u) for u in req.urls)
            return await super().crawl(req)

    class LastTwoReranker:
        async def rank(self, goal, results, top_k, *, time_range=None, usage=None):
            return list(reversed(results))[:top_k]

    engine = DeepResearchEngine(
        searxng=searxng,
        crawler=TrackingCrawler(),
        llm=FakeLLM(),
        reranker=LastTwoReranker(),
        concurrency=1,
        results_per_query=8,
        crawl_top_k=2,
    )
    await engine.run("topic", breadth=1, depth=1)

    assert searxng.max_results == [8]
    # The reranker's choice, not the search engine's order, decides what is fetched.
    assert crawled == ["https://s7.com/q1a", "https://s6.com/q1a"]


@pytest.mark.asyncio
async def test_progress_hook_is_called() -> None:
    events: list[ResearchProgress] = []

    async def on_progress(p: ResearchProgress) -> None:
        events.append(p)

    await _engine().run("topic", breadth=2, depth=1, on_progress=on_progress)
    assert events
    assert {e.stage for e in events} == {"searching", "processed"}


def _override(searxng, crawler, llm):
    app.dependency_overrides[get_searxng] = lambda: searxng
    app.dependency_overrides[get_crawler] = lambda: crawler
    app.dependency_overrides[get_llm] = lambda: llm
    # The app builds an LLM reranker at startup by default; the fake LLM has to
    # stand in for it too, or the route reaches for a real provider.
    app.dependency_overrides[get_reranker] = lambda: NoopReranker()


def test_endpoint_interactive_true_returns_questions() -> None:
    _override(FakeSearxng(), FakeCrawler(), FakeLLM(feedback_questions=["What scope?"]))
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research", json={"query": "x", "interactive": True, "depth": 1, "breadth": 2}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "needs_clarification"
        assert body["questions"] == ["What scope?"]
        assert body["report"] is None
    finally:
        app.dependency_overrides.clear()


def test_endpoint_interactive_false_runs_immediately() -> None:
    # Feedback questions exist, but interactive=false must skip the clarification step.
    _override(FakeSearxng(), FakeCrawler(), FakeLLM(feedback_questions=["ignored?"]))
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research",
                json={"query": "x", "interactive": False, "depth": 1, "breadth": 2},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert "## Sources" in body["report"]
    finally:
        app.dependency_overrides.clear()


def test_endpoint_auto_with_clear_query_runs() -> None:
    # Auto + empty feedback -> proceed to research.
    _override(FakeSearxng(), FakeCrawler(), FakeLLM(feedback_questions=[]))
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/research", json={"query": "x", "depth": 1, "breadth": 2})
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("query", "specific"),
    [
        ("x", False),
        ("what is a good laptop", False),
        ("compare the 2026 energy storage subsidies in Germany and in France", True),
        ("tell me about batteries and solar and stuff in Europe over the last decade", False),
    ],
)
def test_query_looks_specific(query: str, specific: bool) -> None:
    assert query_looks_specific(query) is specific


def test_endpoint_auto_skips_the_clarification_call_for_a_specific_query() -> None:
    """§4.4: a specific query must not pay an LLM round-trip to be told 'no questions'."""
    llm = FakeLLM(feedback_questions=["this must never be asked"])
    _override(FakeSearxng(), FakeCrawler(), llm)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research",
                json={
                    "query": "compare the 2026 energy storage subsidies in Germany and France",
                    "depth": 1,
                    "breadth": 1,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert body["citations"]
        assert body["citations"][0]["index"] == 1
    finally:
        app.dependency_overrides.clear()


def test_endpoint_interactive_true_still_asks_a_specific_query() -> None:
    """The heuristic only skips in "auto"; an explicit interactive=true always asks."""
    _override(FakeSearxng(), FakeCrawler(), FakeLLM(feedback_questions=["Which market?"]))
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research",
                json={
                    "query": "compare the 2026 energy storage subsidies in Germany and France",
                    "interactive": True,
                    "depth": 1,
                    "breadth": 1,
                },
            )
        assert resp.json()["status"] == "needs_clarification"
    finally:
        app.dependency_overrides.clear()


def test_endpoint_with_answers_skips_feedback_and_runs() -> None:
    _override(FakeSearxng(), FakeCrawler(), FakeLLM(feedback_questions=["should be skipped?"]))
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research",
                json={"query": "x", "answers": ["focus on 2026"], "depth": 1, "breadth": 2},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
    finally:
        app.dependency_overrides.clear()
