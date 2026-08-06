import asyncio

import pytest
from starlette.testclient import TestClient

from app.api.deps import get_crawler, get_llm, get_searxng
from app.core.research.engine import DeepResearchEngine, ResearchProgress
from app.core.research.schemas import (
    FeedbackQuestions,
    ReportDraft,
    SerpProcessing,
    SerpQuery,
    SerpQueryList,
)
from app.main import app
from app.models.crawl import CrawledPage
from app.models.search import SearchResponse, SearchResultItem


class FakeSearxng:
    async def search(self, req) -> SearchResponse:
        items = [
            SearchResultItem(title="A", url="https://a.com", snippet="sa"),
            SearchResultItem(title="B", url="https://b.com", snippet="sb"),
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
                learnings=["learning X", "learning Y"], follow_up_questions=["next?"]
            )
        if name == "ReportDraft":
            return ReportDraft(report_markdown="# Report\n\nDetailed body.")
        raise AssertionError(f"unexpected response_model {name}")

    async def complete(self, messages, **kwargs) -> str:  # pragma: no cover
        return ""


def _engine(llm=None, concurrency=2):
    return DeepResearchEngine(
        searxng=FakeSearxng(), crawler=FakeCrawler(), llm=llm or FakeLLM(), concurrency=concurrency
    )


@pytest.mark.asyncio
async def test_run_depth_one_aggregates_and_appends_sources() -> None:
    engine = _engine()
    result = await engine.run("topic", breadth=2, depth=1)

    assert result.learnings == ["learning X", "learning Y"]  # deduped across branches
    assert "## Sources" in result.report
    assert result.report.startswith("# Report")
    assert set(result.sources) == {"https://a.com/", "https://b.com/"}
    assert result.serp_queries == ["q1a", "q1b"]


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
                    learnings=[f"learning {i} " + "word " * 50 for i in range(20)],
                    follow_up_questions=[],
                )
            if response_model.__name__ == "ReportDraft":
                captured.append(messages[-1]["content"].split("\n"))
            return await super().structured(messages, response_model, model=model, **kwargs)

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
            resp = client.post(
                "/v1/research", json={"query": "x", "depth": 1, "breadth": 2}
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
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
