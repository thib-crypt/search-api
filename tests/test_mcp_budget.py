"""The context budget (§3): no tool result may swamp the agent's window.

The number this file defends is `MCP_MAX_RESULT_TOKENS`. Everything else in the
budget design — truncating from the top, marking the cut, parking the remainder
as a resource, sharing one global budget across pages — exists to make that
ceiling survivable rather than merely enforced.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.llm.tokens import count_tokens
from app.main import app
from app.mcp.artifacts import ArtifactPage, ArtifactStore
from app.mcp.budget import budget_pages
from app.models.crawl import CrawledPage, CrawlRequest, SiteCrawlRequest
from tests.mcp_client import mcp_session, resource_links, text_of

# 500 kB of prose, i.e. the "one page of documentation" case from §9.
HUGE = ("Postgres vacuums the heap in passes, and each pass rereads the map. " * 8000)[:500_000]


class BigPageCrawler:
    def __init__(self, pages: list[CrawledPage]) -> None:
        self._pages = pages
        self.requests: list[CrawlRequest] = []

    async def crawl(self, req: CrawlRequest) -> list[CrawledPage]:
        self.requests.append(req)
        return self._pages[: len(req.urls)]

    async def crawl_site(self, req: SiteCrawlRequest, emit=None) -> list[CrawledPage]:
        return self._pages

    async def close(self) -> None:
        pass


async def test_a_500kb_page_is_cut_to_the_budget_and_says_so() -> None:
    crawler = BigPageCrawler(
        [CrawledPage(url="https://example.com/big", success=True, title="Big", markdown=HUGE)]
    )
    async with mcp_session(app) as session:
        app.state.crawler = crawler
        # Asking for a huge slice: the instance budget still has the last word.
        result = await session.call_tool(
            "fetch_url", {"urls": ["https://example.com/big"], "max_chars_per_page": 100_000}
        )

    body = text_of(result)
    budget = get_settings().mcp_max_result_tokens
    assert await count_tokens(body) <= budget
    # The cut is stated, with the number of characters and where the rest lives.
    assert "[truncated:" in body
    assert "of 500,000 characters" in body
    links = resource_links(result)
    assert len(links) == 1 and links[0].startswith("search-api://artifact/")


async def test_the_truncated_remainder_is_readable_as_a_resource() -> None:
    crawler = BigPageCrawler(
        [CrawledPage(url="https://example.com/big", success=True, title="Big", markdown=HUGE)]
    )
    async with mcp_session(app) as session:
        app.state.crawler = crawler
        result = await session.call_tool("fetch_url", {"urls": ["https://example.com/big"]})
        uri = resource_links(result)[0]
        contents = await session.read_resource(uri)
        page = await session.read_resource(f"{uri}/page/1")

    assert HUGE[:200] in contents.contents[0].text
    assert len(page.contents[0].text) > 400_000


async def test_the_budget_is_shared_across_pages_not_multiplied() -> None:
    """Ten pages get one budget between them, not ten budgets."""
    pages = [
        CrawledPage(
            url=f"https://example.com/{index}", success=True, title=f"P{index}", markdown=HUGE
        )
        for index in range(10)
    ]
    async with mcp_session(app) as session:
        app.state.crawler = BigPageCrawler(pages)
        result = await session.call_tool(
            "fetch_url", {"urls": [f"https://example.com/{i}" for i in range(10)]}
        )

    body = text_of(result)
    assert await count_tokens(body) <= get_settings().mcp_max_result_tokens
    # Every page is still represented — a shared budget must not drop pages.
    for index in range(10):
        assert f"P{index}" in body


async def test_a_failed_page_is_reported_and_does_not_hold_a_share() -> None:
    pages = [
        CrawledPage(url="https://example.com/ok", success=True, title="OK", markdown=HUGE),
        CrawledPage(url="https://example.com/bad", success=False, error="403 Forbidden"),
    ]
    async with mcp_session(app) as session:
        app.state.crawler = BigPageCrawler(pages)
        result = await session.call_tool(
            "fetch_url", {"urls": ["https://example.com/ok", "https://example.com/bad"]}
        )

    body = text_of(result)
    assert "403 Forbidden" in body
    assert result.structured_content is not None
    assert result.structured_content["pages"][1]["success"] is False


async def test_a_small_result_gets_no_artifact_link() -> None:
    """A pointer to text the agent already has is noise, not a feature."""
    crawler = BigPageCrawler(
        [CrawledPage(url="https://example.com/s", success=True, title="S", markdown="short")]
    )
    async with mcp_session(app) as session:
        app.state.crawler = crawler
        result = await session.call_tool("fetch_url", {"urls": ["https://example.com/s"]})
    assert resource_links(result) == []


# -- the budgeting primitive on its own -------------------------------------


async def test_truncation_keeps_the_head_of_the_page() -> None:
    store = ArtifactStore(ttl=60, max_items=8)
    page = ArtifactPage(title="T", text="START " + ("x" * 100_000) + " END")
    result = await budget_pages(tool="t", pages=[page], store=store, max_result_tokens=200)
    text = result.blocks[0].text  # type: ignore[union-attr]
    assert "START" in text
    assert "END" not in text


async def test_an_artifact_is_dropped_once_its_ttl_has_passed() -> None:
    import asyncio

    store = ArtifactStore(ttl=0.01, max_items=8)
    artifact = await store.put("t", [ArtifactPage(title="T", text="body")])
    assert await store.get(artifact.id) is not None
    await asyncio.sleep(0.02)
    assert await store.get(artifact.id) is None


async def test_the_store_evicts_least_recently_used_first() -> None:
    store = ArtifactStore(ttl=600, max_items=2)
    first = await store.put("t", [ArtifactPage(title="1", text="a")])
    second = await store.put("t", [ArtifactPage(title="2", text="b")])
    await store.get(first.id)  # first is now the most recently used
    third = await store.put("t", [ArtifactPage(title="3", text="c")])
    assert await store.get(second.id) is None
    assert await store.get(first.id) is not None
    assert await store.get(third.id) is not None


@pytest.mark.parametrize("uri", ["search-api://artifact/nope", "search-api://artifact/nope/page/2"])
async def test_a_missing_artifact_reads_as_an_explanation(uri: str) -> None:
    async with mcp_session(app) as session:
        contents = await session.read_resource(uri)
    assert "No artifact" in contents.contents[0].text
