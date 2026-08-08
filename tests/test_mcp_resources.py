"""Resources, prompts, elicitation and cancellation (§4, §5, §7)."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.client.session import ClientRequestContext
from mcp.types import ElicitRequestFormParams, ElicitResult, ErrorData

from app.config import get_settings
from app.main import app
from tests.mcp_client import (
    install_stub_engine,
    live_mcp_session,
    mcp_session,
    text_of,
)
from tests.test_mcp_tools import FakeCrawler

# -- resources --------------------------------------------------------------


async def test_capabilities_publishes_the_limits_actually_enforced() -> None:
    async with mcp_session(app) as session:
        contents = await session.read_resource("search-api://capabilities")
    payload = json.loads(contents.contents[0].text)

    settings = get_settings()
    assert payload["context_budget"]["max_result_tokens"] == settings.mcp_max_result_tokens
    assert payload["limits"]["fetch_url.urls"] == settings.mcp_max_urls_per_call
    assert payload["limits"]["deep_research.breadth"] == 6
    # The per-tool cost table is what lets an agent pace itself.
    assert payload["rate_limit"]["tool_costs"]["deep_research"] == 100
    assert payload["rate_limit"]["tool_costs"]["web_search"] == 1


async def test_capabilities_reports_the_remaining_daily_budget(monkeypatch) -> None:
    monkeypatch.setenv("DAILY_COST_CAP_USD", "5.0")
    get_settings.cache_clear()
    try:
        async with mcp_session(app) as session:
            await app.state.cost_ledger.add("anonymous", 1.25)
            contents = await session.read_resource("search-api://capabilities")
        payload = json.loads(contents.contents[0].text)
        assert payload["daily_cost_cap_usd"]["spent_today"] == pytest.approx(1.25)
        assert payload["daily_cost_cap_usd"]["remaining"] == pytest.approx(3.75)
    finally:
        get_settings.cache_clear()


async def test_the_resource_templates_are_advertised() -> None:
    async with mcp_session(app) as session:
        templates = await session.list_resource_templates()
    advertised = {template.uri_template for template in templates.resource_templates}
    assert advertised == {
        "search-api://artifact/{artifact_id}",
        "search-api://artifact/{artifact_id}/page/{page_number}",
        "search-api://job/{job_id}",
    }


async def test_a_job_is_readable_as_a_resource() -> None:
    async with mcp_session(app) as session:
        app.state.crawler = FakeCrawler()
        started = await session.call_tool("crawl_site", {"url": "https://example.com/"})
        job_id = started.structured_content["job_id"]
        contents = await session.read_resource(f"search-api://job/{job_id}")
    payload = json.loads(contents.contents[0].text)
    assert payload["id"] == job_id
    assert payload["type"] == "site_crawl"


# -- prompts ----------------------------------------------------------------


async def test_the_two_prompts_are_offered_and_render() -> None:
    async with mcp_session(app) as session:
        listed = await session.list_prompts()
        brief = await session.get_prompt("research_brief", {"topic": "vector databases"})
        verify = await session.get_prompt("verify_claim", {"claim": "HNSW is always faster"})

    assert {prompt.name for prompt in listed.prompts} == {"research_brief", "verify_claim"}
    brief_text = brief.messages[0].content.text
    assert "vector databases" in brief_text
    assert "deep_research_estimate" in brief_text
    verify_text = verify.messages[0].content.text
    assert "HNSW is always faster" in verify_text
    # The point of this prompt is to *avoid* the paid tool.
    assert "Do not use `answer`" in verify_text


# -- elicitation (§5) -------------------------------------------------------


@pytest.fixture
def stub_engine(monkeypatch):
    """A research engine that always asks one clarifying question."""
    return install_stub_engine(monkeypatch)


async def test_without_elicitation_the_questions_come_back_as_the_result(stub_engine) -> None:
    """The HTTP behaviour: the agent is told to call again with `answers`."""
    async with mcp_session(app) as session:
        result = await session.call_tool("deep_research", {"query": "databases"})

    assert result.structured_content is not None
    assert result.structured_content["status"] == "needs_clarification"
    assert result.structured_content["questions"] == ["Which database version are you targeting?"]
    assert "`answers` array" in text_of(result)


async def test_with_elicitation_the_answers_fold_into_the_same_call(stub_engine) -> None:
    """The agent sees one deep_research that worked, not a call it has to retry."""

    async def answer_the_form(
        context: ClientRequestContext, params: ElicitRequestFormParams
    ) -> ElicitResult | ErrorData:
        return ElicitResult(action="accept", content={"answer_1": "Postgres 17"})

    async with live_mcp_session(app, elicitation_callback=answer_the_form) as session:
        result = await session.call_tool("deep_research", {"query": "databases"})

    assert not result.is_error
    assert result.structured_content is not None
    # It launched a run rather than asking again.
    assert result.structured_content["status"] == "pending"
    assert result.structured_content["answers_used"] == [
        "Which database version are you targeting? Postgres 17"
    ]
    await _settle()
    assert "Postgres 17" in stub_engine.queries[0]


async def test_a_declined_form_researches_the_question_as_it_stands(stub_engine) -> None:
    """Declining to elaborate is not declining the research."""

    async def decline(
        context: ClientRequestContext, params: ElicitRequestFormParams
    ) -> ElicitResult | ErrorData:
        return ElicitResult(action="decline")

    async with live_mcp_session(app, elicitation_callback=decline) as session:
        result = await session.call_tool("deep_research", {"query": "databases"})

    assert not result.is_error
    assert result.structured_content is not None
    assert result.structured_content["status"] == "pending"
    assert "answers_used" not in result.structured_content


# -- cancellation -----------------------------------------------------------


class SlowCrawler(FakeCrawler):
    async def crawl_site(self, req, emit=None):
        await asyncio.sleep(30)
        return []


async def test_a_job_started_from_mcp_can_be_cancelled(monkeypatch) -> None:
    """The MCP job id *is* the job id, so the existing cancellation applies."""
    async with mcp_session(app) as session:
        app.state.crawler = SlowCrawler()
        started = await session.call_tool("crawl_site", {"url": "https://example.com/"})
        job_id = started.structured_content["job_id"]

        assert app.state.jobs.cancel(job_id) is True
        await _settle()

        collected = await session.call_tool("job_status", {"job_id": job_id})

    assert collected.structured_content is not None
    assert collected.structured_content["status"] == "failed"
    assert collected.structured_content["error"] == "cancelled"


async def _settle(attempts: int = 20) -> None:
    for _ in range(attempts):
        await asyncio.sleep(0.02)
