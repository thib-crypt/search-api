"""Research streaming and SERP filters (§5.6), and the /v1/search domain filters."""

from __future__ import annotations

import json

import httpx
import respx
from starlette.testclient import TestClient

from app.api.deps import get_crawler, get_jobs, get_llm, get_reranker, get_searxng
from app.core.research.engine import DeepResearchEngine
from app.core.research.rerank import NoopReranker
from app.jobs.manager import JobManager
from app.main import app
from app.models.search import SearchRequest, SearchResponse
from tests.test_research import FakeCrawler, FakeLLM, FakeSearxng

# --- SearXNG-level domain filtering ---------------------------------------

MIXED_JSON = {
    "results": [
        {"url": "https://good.example/a", "title": "A", "content": ""},
        {"url": "https://www.good.example/b", "title": "B", "content": ""},
        {"url": "https://spam.example/c", "title": "C", "content": ""},
    ]
}


@respx.mock
def test_search_include_domains_keeps_only_matching_hosts() -> None:
    respx.get("http://searxng:8080/search").mock(return_value=httpx.Response(200, json=MIXED_JSON))
    with TestClient(app) as client:
        resp = client.post("/v1/search", json={"q": "x", "include_domains": ["good.example"]})
    urls = [r["url"] for r in resp.json()["results"]]
    # Subdomains count as a match, exactly like the SSRF guard's allow-list.
    assert urls == ["https://good.example/a", "https://www.good.example/b"]


@respx.mock
def test_search_exclude_domains_drops_matching_hosts() -> None:
    respx.get("http://searxng:8080/search").mock(return_value=httpx.Response(200, json=MIXED_JSON))
    with TestClient(app) as client:
        resp = client.post("/v1/search", json={"q": "x", "exclude_domains": ["spam.example"]})
    urls = [r["url"] for r in resp.json()["results"]]
    assert urls == ["https://good.example/a", "https://www.good.example/b"]


@respx.mock
def test_domain_filter_is_applied_before_max_results() -> None:
    respx.get("http://searxng:8080/search").mock(return_value=httpx.Response(200, json=MIXED_JSON))
    with TestClient(app) as client:
        resp = client.post(
            "/v1/search",
            json={"q": "x", "exclude_domains": ["good.example"], "max_results": 2},
        )
    # Only one result survives the filter; truncation must not eat it first.
    assert [r["url"] for r in resp.json()["results"]] == ["https://spam.example/c"]


@respx.mock
def test_different_domain_filters_do_not_share_a_cache_entry() -> None:
    route = respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=MIXED_JSON)
    )
    with TestClient(app) as client:
        first = client.post("/v1/search", json={"q": "x", "include_domains": ["good.example"]})
        second = client.post("/v1/search", json={"q": "x", "include_domains": ["spam.example"]})
    assert route.call_count >= 1
    assert [r["url"] for r in first.json()["results"]] != [
        r["url"] for r in second.json()["results"]
    ]


# --- Engine filter threading ----------------------------------------------


class RecordingSearxng(FakeSearxng):
    def __init__(self) -> None:
        self.requests: list[SearchRequest] = []

    async def search(self, req) -> SearchResponse:
        self.requests.append(req)
        return await super().search(req)


async def test_engine_threads_filters_into_every_search() -> None:
    searxng = RecordingSearxng()
    engine = DeepResearchEngine(searxng=searxng, crawler=FakeCrawler(), llm=FakeLLM())

    await engine.run(
        "topic",
        breadth=2,
        depth=2,
        time_range="week",
        include_domains=["a.com"],
        exclude_domains=["b.com"],
    )

    assert searxng.requests, "the run performed no search"
    # Every level, not just the first.
    assert all(r.time_range == "week" for r in searxng.requests)
    assert all(r.include_domains == ["a.com"] for r in searxng.requests)
    assert all(r.exclude_domains == ["b.com"] for r in searxng.requests)


async def test_domain_filter_actually_removes_sources_from_a_run() -> None:
    """The filter is enforced by the SearXNG client, so a real one is used here."""

    class FilteringSearxng:
        async def search(self, req) -> SearchResponse:
            data = {
                "results": [
                    {"url": "https://a.com/x", "title": "A", "content": ""},
                    {"url": "https://b.com/x", "title": "B", "content": ""},
                ]
            }
            from app.core.search.searxng import SearxngClient

            return SearxngClient._parse(req, data)

    engine = DeepResearchEngine(
        searxng=FilteringSearxng(), crawler=FakeCrawler(), llm=FakeLLM(), reranker=NoopReranker()
    )
    result = await engine.run("topic", breadth=1, depth=1, exclude_domains=["b.com"])

    assert result.sources == ["https://a.com/x"]


# --- Report streaming ------------------------------------------------------


class StreamingLLM(FakeLLM):
    """Adds the token-level API `complete` alone cannot provide."""

    def __init__(self) -> None:
        super().__init__(feedback_questions=[])
        self.streamed = False

    async def stream_complete(self, messages, **kwargs):
        self.streamed = True
        for token in ("# Report", "\n\n", "body"):
            yield token


async def test_engine_streams_report_tokens_when_a_hook_is_given() -> None:
    llm = StreamingLLM()
    engine = DeepResearchEngine(searxng=FakeSearxng(), crawler=FakeCrawler(), llm=llm)
    tokens: list[str] = []

    async def on_token(token: str) -> None:
        tokens.append(token)

    result = await engine.run("topic", breadth=1, depth=1, on_token=on_token)

    assert llm.streamed is True
    assert tokens == ["# Report", "\n\n", "body"]
    # The assembled report is the concatenation, plus the usual source appendix.
    assert result.report.startswith("# Report\n\nbody")


async def test_report_is_not_streamed_without_a_hook() -> None:
    llm = StreamingLLM()
    engine = DeepResearchEngine(searxng=FakeSearxng(), crawler=FakeCrawler(), llm=llm)

    await engine.run("topic", breadth=1, depth=1)

    assert llm.streamed is False  # the plain `complete` path is untouched


def _override_research(jobs: JobManager, llm) -> None:
    app.dependency_overrides[get_searxng] = lambda: FakeSearxng()
    app.dependency_overrides[get_crawler] = lambda: FakeCrawler()
    app.dependency_overrides[get_llm] = lambda: llm
    app.dependency_overrides[get_reranker] = lambda: NoopReranker()
    app.dependency_overrides[get_jobs] = lambda: jobs


def test_stream_true_runs_as_a_job_and_emits_report_tokens() -> None:
    jobs = JobManager()
    llm = StreamingLLM()
    _override_research(jobs, llm)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research",
                json={"query": "x", "stream": True, "depth": 1, "breadth": 1},
            )
            assert resp.status_code == 200
            job_id = resp.json()["job_id"]
            assert resp.json()["stream_url"] == f"/v1/jobs/{job_id}/stream"

            # The SSE channel replays the job's events once it is done.
            with client.stream("GET", f"/v1/jobs/{job_id}/stream") as stream:
                payloads = [
                    json.loads(line[len("data: ") :])
                    for line in stream.iter_lines()
                    if line.startswith("data: ")
                ]

        tokens = [p["token"] for p in payloads if p.get("stage") == "report_token"]
        assert tokens == ["# Report", "\n\n", "body"]
        # Token events ride the same {"event": "progress"} envelope as the rest.
        assert all(p["event"] == "progress" for p in payloads if p.get("stage") == "report_token")
        assert any(p.get("event") == "completed" for p in payloads)
    finally:
        app.dependency_overrides.clear()


def test_background_without_stream_emits_no_token_events() -> None:
    jobs = JobManager()
    llm = StreamingLLM()
    _override_research(jobs, llm)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research",
                json={"query": "x", "background": True, "depth": 1, "breadth": 1},
            )
            job_id = resp.json()["job_id"]
            with client.stream("GET", f"/v1/jobs/{job_id}/stream") as stream:
                body = "".join(stream.iter_lines())

        assert "report_token" not in body
        assert llm.streamed is False
    finally:
        app.dependency_overrides.clear()
