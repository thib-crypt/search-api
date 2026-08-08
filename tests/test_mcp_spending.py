"""The tools that spend model credits, and the paths around them (§2 tier 2-3).

`answer` and `extract_structured` reach the same services `POST /v1/answer` and
`POST /v1/extract` do — the point of §0 — so what is worth testing here is what
the MCP layer adds on top: the label the spend is billed to, the budgeting of the
result, and the refusals.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.main import app
from app.models.crawl import CrawledPage, CrawlRequest, SiteCrawlRequest
from app.models.search import SearchResponse, SearchResultItem
from tests.mcp_client import install_stub_engine, mcp_session, text_of


class FakeSearxng:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def search(self, req) -> SearchResponse:
        self.requests.append(req)
        items = [
            SearchResultItem(title="First", url="https://a.example", snippet="snippet a"),
            SearchResultItem(title="Second", url="https://b.example", snippet="snippet b"),
        ]
        return SearchResponse(query=req.q, number_of_results=len(items), results=items)

    async def aclose(self) -> None:
        pass


class FakeCrawler:
    async def crawl(self, req: CrawlRequest) -> list[CrawledPage]:
        return [
            CrawledPage(
                url=str(url),
                success=True,
                status_code=200,
                title="A page",
                markdown=f"full text of {url}",
            )
            for url in req.urls
        ]

    async def crawl_site(self, req: SiteCrawlRequest, emit=None) -> list[CrawledPage]:
        return []

    async def close(self) -> None:
        pass


class FakeLLM:
    default_model = "test/model"

    async def complete(self, messages, *, model=None, **kwargs) -> str:
        return "Synthesized answer citing [1] and [2]."

    async def structured(self, messages, response_model, *, model=None, usage=None, **kwargs):
        return response_model.model_validate({"price_usd": 42.0})


@pytest.fixture
def fakes():
    """Install the fake clients once the app has started."""

    def install() -> FakeSearxng:
        searxng = FakeSearxng()
        app.state.searxng = searxng
        app.state.crawler = FakeCrawler()
        app.state.llm = FakeLLM()
        return searxng

    return install


async def test_answer_returns_the_answer_and_its_sources(fakes) -> None:
    async with mcp_session(app) as session:
        fakes()
        result = await session.call_tool("answer", {"query": "what is x?", "max_sources": 2})

    assert not result.is_error
    body = text_of(result)
    assert "Synthesized answer" in body
    assert "[1] First — https://a.example" in body
    assert result.structured_content is not None
    assert result.structured_content["model"] == "test/model"
    # `model` is not an argument: letting an agent pick the model is letting it
    # pick the bill (§2).
    assert "model" not in result.structured_content.get("arguments", {})


async def test_answer_bills_the_caller_label(fakes) -> None:
    """Even a run that spends nothing must close its books, as the HTTP route does."""
    async with mcp_session(app) as session:
        fakes()
        await session.call_tool("answer", {"query": "what is x?"})
        spent = await app.state.cost_ledger.spent_today("anonymous")
    assert spent >= 0.0


async def test_extract_structured_returns_typed_fields(fakes) -> None:
    schema = {"type": "object", "properties": {"price_usd": {"type": "number"}}}
    async with mcp_session(app) as session:
        fakes()
        result = await session.call_tool(
            "extract_structured", {"urls": ["https://shop.example/p"], "schema": schema}
        )

    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["count"] == 1
    assert result.structured_content["results"][0]["data"] == {"price_usd": 42.0}


async def test_extract_structured_refuses_a_schema_that_is_not_an_object(fakes) -> None:
    async with mcp_session(app) as session:
        fakes()
        result = await session.call_tool(
            "extract_structured",
            {"urls": ["https://shop.example/p"], "schema": "just a sentence"},
        )
    assert result.is_error


async def test_extract_structured_caps_the_url_list(fakes) -> None:
    async with mcp_session(app) as session:
        fakes()
        result = await session.call_tool(
            "extract_structured",
            {
                "urls": [f"https://shop.example/{i}" for i in range(9)],
                "schema": {"type": "object"},
            },
        )
    assert result.is_error


async def test_web_search_surfaces_a_backend_failure(monkeypatch) -> None:
    class BrokenSearxng(FakeSearxng):
        async def search(self, req):
            from app.core.search.searxng import SearxngError

            raise SearxngError("searxng is down")

    async with mcp_session(app) as session:
        app.state.searxng = BrokenSearxng()
        result = await session.call_tool("web_search", {"q": "anything"})
    assert result.is_error
    assert "Search backend unavailable" in text_of(result)


async def test_map_site_refuses_a_url_the_guard_blocks(fakes) -> None:
    async with mcp_session(app) as session:
        fakes()
        result = await session.call_tool("map_site", {"url": "not-a-url"})
    assert result.is_error


async def test_a_finished_research_job_comes_back_budgeted(monkeypatch) -> None:
    """A report is text, so `job_status` budgets it like any other tool result."""
    install_stub_engine(monkeypatch)
    async with mcp_session(app) as session:
        started = await session.call_tool(
            "deep_research", {"query": "pgvector index builds", "answers": ["as stated"]}
        )
        job_id = started.structured_content["job_id"]
        collected = await _poll(session, job_id)

    assert collected.structured_content is not None
    assert collected.structured_content["status"] == "completed"
    assert collected.structured_content["query"] == "pgvector index builds"
    assert "done" in text_of(collected)


async def test_job_status_can_skip_the_result(monkeypatch) -> None:
    install_stub_engine(monkeypatch)
    async with mcp_session(app) as session:
        started = await session.call_tool(
            "deep_research", {"query": "pgvector index builds", "answers": ["as stated"]}
        )
        job_id = started.structured_content["job_id"]
        await _poll(session, job_id)
        summary = await session.call_tool("job_status", {"job_id": job_id, "include_result": False})

    assert "include_result=true" in text_of(summary)
    assert "result" not in (summary.structured_content or {})


async def test_a_running_job_reports_its_progress(monkeypatch) -> None:
    install_stub_engine(monkeypatch, run_seconds=5.0)
    async with mcp_session(app) as session:
        started = await session.call_tool(
            "deep_research", {"query": "pgvector index builds", "answers": ["as stated"]}
        )
        job_id = started.structured_content["job_id"]
        status = await session.call_tool("job_status", {"job_id": job_id})

    assert status.structured_content is not None
    assert status.structured_content["status"] in ("pending", "running")
    assert status.structured_content["wait_seconds_between_polls"] == 20
    assert "Call job_status again" in text_of(status)


# -- screenshots and PDFs (§3) ---------------------------------------------


class CapturingCrawler(FakeCrawler):
    async def crawl(self, req: CrawlRequest) -> list[CrawledPage]:
        return [
            CrawledPage(
                url=str(url),
                success=True,
                title="Shot",
                markdown="text",
                screenshot="iVBORw0KGgo=",
            )
            for url in req.urls
        ]


async def test_capture_is_not_offered_when_image_mode_is_off() -> None:
    """The default: the parameter does not exist, so it cannot be filled in."""
    async with mcp_session(app) as session:
        listed = await session.list_tools()
    fetch = next(tool for tool in listed.tools if tool.name == "fetch_url")
    assert "capture" not in fetch.input_schema["properties"]


async def test_a_screenshot_comes_back_as_a_resource_not_inline(monkeypatch) -> None:
    monkeypatch.setenv("MCP_IMAGE_MODE", "resource")
    get_settings.cache_clear()
    try:
        async with mcp_session(app) as session:
            app.state.crawler = CapturingCrawler()
            listed = await session.list_tools()
            fetch = next(tool for tool in listed.tools if tool.name == "fetch_url")
            assert "capture" in fetch.input_schema["properties"]
            result = await session.call_tool(
                "fetch_url",
                {"urls": ["https://example.com/a"], "capture": "screenshot"},
            )
        # A link, never base64 in the transcript.
        links = [block for block in result.content if block.type == "resource_link"]
        assert any(link.mime_type == "image/png" for link in links)
        assert "iVBORw0KGgo=" not in text_of(result)
    finally:
        get_settings.cache_clear()


async def test_an_inline_screenshot_is_only_sent_when_asked_for(monkeypatch) -> None:
    monkeypatch.setenv("MCP_IMAGE_MODE", "inline")
    get_settings.cache_clear()
    try:
        async with mcp_session(app) as session:
            app.state.crawler = CapturingCrawler()
            result = await session.call_tool(
                "fetch_url", {"urls": ["https://example.com/a"], "capture": "screenshot"}
            )
        images = [block for block in result.content if block.type == "image"]
        assert len(images) == 1
        assert images[0].mime_type == "image/png"
    finally:
        get_settings.cache_clear()


async def test_capture_is_refused_once_the_operator_turns_images_off(monkeypatch) -> None:
    """Settings are re-read per call, so the mode can change under a live server.

    The parameter is advertised because the instance started with images on; a
    client holding that schema must get a refusal it can read, not a screenshot
    the operator has disabled.
    """
    monkeypatch.setenv("MCP_IMAGE_MODE", "resource")
    get_settings.cache_clear()
    try:
        async with mcp_session(app) as session:
            app.state.crawler = CapturingCrawler()
            monkeypatch.setenv("MCP_IMAGE_MODE", "off")
            get_settings.cache_clear()
            result = await session.call_tool(
                "fetch_url", {"urls": ["https://example.com/a"], "capture": "screenshot"}
            )
        assert result.is_error
        assert "MCP_IMAGE_MODE=off" in text_of(result)
    finally:
        get_settings.cache_clear()


# -- stdio (§8) -------------------------------------------------------------


def test_stdio_keeps_stdout_clean() -> None:
    """One log line on stdout is a corrupt JSON-RPC frame, and stdio never recovers."""
    import logging
    import sys

    import app.mcp.__main__ as stdio

    noisy = logging.getLogger("test.noisy.stdout")
    handler = logging.StreamHandler(sys.stdout)
    noisy.addHandler(handler)
    try:
        stdio._logs_to_stderr()
        assert handler.stream is sys.stderr
        for existing in logging.getLogger().handlers:
            if isinstance(existing, logging.StreamHandler):
                assert existing.stream is not sys.stdout
    finally:
        noisy.removeHandler(handler)


async def test_the_stdio_entrypoint_builds_the_same_server(monkeypatch) -> None:
    """`python -m app.mcp` must expose the same nine tools, not a second surface."""
    import app.mcp.__main__ as stdio

    captured: dict[str, object] = {}

    async def fake_run_stdio(self) -> None:
        captured["tools"] = sorted((await self.list_tools()), key=lambda tool: tool.name)

    monkeypatch.setattr("mcp.server.MCPServer.run_stdio_async", fake_run_stdio, raising=False)
    await stdio._serve()

    names = {tool.name for tool in captured["tools"]}  # type: ignore[union-attr]
    assert len(names) == 9
    assert "deep_research" in names


async def _poll(session, job_id: str, attempts: int = 60):
    import asyncio

    for _ in range(attempts):
        result = await session.call_tool("job_status", {"job_id": job_id})
        if (result.structured_content or {}).get("status") not in ("pending", "running"):
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")
