"""The nine tools, called over a real MCP session (§9).

Wiring first: a tool that is registered but wired to the wrong service fails
here, not in production. The budget, auth and rate-limit behaviours have their
own files.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.main import app
from app.models.crawl import CrawledPage, CrawlRequest, SiteCrawlRequest
from tests.mcp_client import mcp_session, resource_links, text_of

SEARXNG_JSON = {
    "query": "pgvector hnsw",
    "results": [
        {
            "url": "https://example.com/hnsw",
            "title": "HNSW indexes",
            "content": "Graph-based approximate nearest neighbour search.",
            "engine": "google",
        },
        {
            "url": "https://example.com/ivfflat",
            "title": "IVFFlat indexes",
            "content": "Inverted file with flat compression.",
            "engine": "bing",
        },
    ],
    "suggestions": [],
    "answers": [],
}

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/docs/intro</loc></url>
  <url><loc>https://example.com/docs/api</loc></url>
  <url><loc>https://example.com/pricing</loc></url>
</urlset>"""


class FakeCrawler:
    """Stands in for `CrawlerService`: same two methods the tools call."""

    def __init__(self, pages: list[CrawledPage] | None = None) -> None:
        self.pages = pages or []
        self.requests: list[CrawlRequest] = []
        self.site_requests: list[SiteCrawlRequest] = []

    async def crawl(self, req: CrawlRequest) -> list[CrawledPage]:
        self.requests.append(req)
        if self.pages:
            return self.pages
        return [
            CrawledPage(url=str(url), success=True, title=f"Page {index}", markdown="body")
            for index, url in enumerate(req.urls, start=1)
        ]

    async def crawl_site(self, req: SiteCrawlRequest, emit=None) -> list[CrawledPage]:
        self.site_requests.append(req)
        return [CrawledPage(url=str(req.url), success=True, title="Home", markdown="hello")]

    async def close(self) -> None:
        """Called by the app's shutdown; there is no browser to tear down here."""


@pytest.fixture
def fake_crawler(monkeypatch):
    """Install a fake crawler on app.state once the app has started.

    `McpState` reads through to `app.state` on every access precisely so this
    works: a snapshot taken while the server was built would pin the real
    crawler and every one of these tests would try to start Chromium.
    """
    crawler = FakeCrawler()

    async def install() -> FakeCrawler:
        app.state.crawler = crawler
        return crawler

    return crawler, install


async def test_tools_are_listed() -> None:
    async with mcp_session(app) as session:
        listed = await session.list_tools()
    names = {tool.name for tool in listed.tools}
    assert names == {
        "web_search",
        "map_site",
        "fetch_url",
        "crawl_site",
        "answer",
        "extract_structured",
        "deep_research_estimate",
        "deep_research",
        "job_status",
    }


@respx.mock
async def test_web_search_returns_results_and_structured_content() -> None:
    respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    async with mcp_session(app) as session:
        result = await session.call_tool("web_search", {"q": "pgvector hnsw", "max_results": 5})

    assert not result.is_error
    body = text_of(result)
    assert "https://example.com/hnsw" in body
    assert "Graph-based approximate" in body
    assert result.structured_content is not None
    assert result.structured_content["count"] == 2
    # Small result: nothing was cut, so no artifact link is dangled at the agent.
    assert resource_links(result) == []


@respx.mock
async def test_map_site_lists_sitemap_urls() -> None:
    respx.get("https://example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=SITEMAP)
    )
    async with mcp_session(app) as session:
        result = await session.call_tool(
            "map_site", {"url": "https://example.com/docs/intro", "search": "/docs/"}
        )

    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["source"] == "sitemap"
    assert result.structured_content["links"] == [
        "https://example.com/docs/intro",
        "https://example.com/docs/api",
    ]


async def test_fetch_url_reads_pages(fake_crawler) -> None:
    crawler, install = fake_crawler
    async with mcp_session(app) as session:
        await install()
        result = await session.call_tool(
            "fetch_url", {"urls": ["https://example.com/a", "https://example.com/b"]}
        )

    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["count"] == 2
    assert [str(u) for u in crawler.requests[0].urls] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    # Cleaning knobs are not exposed to the agent; the instance defaults apply.
    assert crawler.requests[0].content_filter == "pruning"


async def test_fetch_url_refuses_more_urls_than_the_instance_allows(fake_crawler) -> None:
    _, install = fake_crawler
    async with mcp_session(app) as session:
        await install()
        result = await session.call_tool(
            "fetch_url", {"urls": [f"https://example.com/{i}" for i in range(30)]}
        )
    assert result.is_error
    assert "at most" in text_of(result)


async def test_crawl_site_starts_a_job_and_job_status_collects_it(fake_crawler) -> None:
    crawler, install = fake_crawler
    async with mcp_session(app) as session:
        await install()
        started = await session.call_tool("crawl_site", {"url": "https://example.com/docs/"})
        assert not started.is_error
        assert started.structured_content is not None
        job_id = started.structured_content["job_id"]
        assert started.structured_content["poll_with"]["tool"] == "job_status"
        # The MCP job id *is* the HTTP job id (§4).
        assert started.structured_content["http_status_url"] == f"/v1/jobs/{job_id}"

        collected = await _poll(session, job_id)

    assert collected.structured_content is not None
    assert collected.structured_content["status"] == "completed"
    assert collected.structured_content["count"] == 1
    assert "hello" in text_of(collected)
    assert crawler.site_requests[0].max_pages == 25


async def test_deep_research_estimate_spends_nothing() -> None:
    async with mcp_session(app) as session:
        result = await session.call_tool(
            "deep_research_estimate", {"query": "state of RAG", "breadth": 4, "depth": 2}
        )
    assert not result.is_error
    assert result.structured_content is not None
    estimate = result.structured_content["estimate"]
    assert estimate["serp_queries"] > 0
    assert estimate["estimated_cost_usd"] >= 0
    assert "cost about" in text_of(result)


async def test_job_status_on_an_unknown_job_says_so() -> None:
    async with mcp_session(app) as session:
        result = await session.call_tool("job_status", {"job_id": "nope"})
    assert result.is_error
    assert "No job" in text_of(result)


async def _poll(session, job_id: str, attempts: int = 40):
    """Poll `job_status` until the job leaves the running state."""
    import asyncio

    for _ in range(attempts):
        result = await session.call_tool("job_status", {"job_id": job_id})
        structured = result.structured_content or {}
        if structured.get("status") not in ("pending", "running"):
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")
