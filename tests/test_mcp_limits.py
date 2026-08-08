"""Rate-limit weighting and cost controls on the MCP surface (§6.2, §6.3).

The gap these cover is the one that would be invisible in production.
`RateLimitMiddleware` prices a request from its *path*, before routing — and
every tool shares the path `/mcp`, so without the top-up in `app.mcp.guards` a
`deep_research` would cost one point instead of a hundred and the budget guard
would guard nothing at all. Nothing would look broken; the bill would just
arrive.
"""

from __future__ import annotations

import pytest

from app.api.middleware import reset_rate_limiter
from app.config import get_settings
from app.core.cost import InMemoryCostLedger
from app.main import app
from app.mcp.guards import TOOL_WEIGHTS
from tests.mcp_client import install_stub_engine, mcp_session, text_of
from tests.test_mcp_tools import FakeCrawler

KEY = "sk_test_" + "e" * 32

# `answers` skips the clarification round-trip, which is a real LLM call these
# tests have no interest in paying for -- they are about what happens *before*
# the run starts.
RESEARCH_ARGS = {"query": "pgvector HNSW build times", "answers": ["as stated"]}


@pytest.fixture(autouse=True)
def _stub_engine(monkeypatch):
    """No test here is about the research engine; none of them should pay for it.

    `run_seconds` keeps a launched run in flight, which is what the per-key
    concurrency bound is measured against — a run that returns instantly frees
    its slot instantly and would make that bound untestable.
    """
    return install_stub_engine(monkeypatch, run_seconds=5.0)


@pytest.fixture
def limited(monkeypatch):
    """Auth on, a 150-point budget: enough for one deep_research, not two."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", f"agent:{KEY}")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_RPM", "150")
    get_settings.cache_clear()
    reset_rate_limiter()
    yield
    get_settings.cache_clear()
    reset_rate_limiter()


async def test_two_deep_research_calls_exceed_a_150_point_budget(limited) -> None:
    async with mcp_session(app, api_key=KEY) as session:
        first = await session.call_tool("deep_research", RESEARCH_ARGS)
        second = await session.call_tool("deep_research", RESEARCH_ARGS)

    assert not first.is_error
    assert second.is_error
    message = text_of(second)
    assert "Rate limit exceeded" in message
    # The refusal has to be actionable: the cost, the budget and the delay.
    assert "100 points" in message
    assert "Retry in" in message


async def test_cheap_tools_are_still_served_under_the_same_budget(limited) -> None:
    """A `deep_research` must not lock out the free tools for the whole window."""
    async with mcp_session(app, api_key=KEY) as session:
        await session.call_tool("deep_research", RESEARCH_ARGS)
        estimate = await session.call_tool(
            "deep_research_estimate", {"query": "how much would that have cost"}
        )
    assert not estimate.is_error


def test_every_tool_has_a_declared_weight() -> None:
    """A new tool without a weight would silently be priced at the default 1."""
    from app.mcp import tools as tools_module

    declared = set(TOOL_WEIGHTS)
    assert declared == {
        "web_search",
        "map_site",
        "fetch_url",
        "answer",
        "extract_structured",
        "crawl_site",
        "deep_research",
        "deep_research_estimate",
        "job_status",
    }
    # And the expensive ones match the HTTP table they mirror.
    assert TOOL_WEIGHTS["deep_research"] == 100
    assert TOOL_WEIGHTS["crawl_site"] == 25
    assert tools_module.MAX_BREADTH == 6


# -- daily cost cap ---------------------------------------------------------


@pytest.fixture
def over_budget(monkeypatch):
    monkeypatch.setenv("DAILY_COST_CAP_USD", "1.0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("answer", {"query": "anything"}),
        ("extract_structured", {"urls": ["https://example.com/p"], "schema": {"type": "object"}}),
        ("deep_research", RESEARCH_ARGS),
    ],
)
async def test_the_spending_tools_refuse_a_key_over_its_daily_cap(
    over_budget, tool: str, arguments: dict
) -> None:
    ledger = InMemoryCostLedger()
    async with mcp_session(app) as session:
        app.state.cost_ledger = ledger
        await ledger.add("anonymous", 2.0)
        result = await session.call_tool(tool, arguments)

    assert result.is_error
    assert "Daily cost cap reached" in text_of(result)


async def test_the_free_tools_keep_working_over_the_cap(over_budget) -> None:
    """A drained budget must not take web_search and job_status down with it."""
    ledger = InMemoryCostLedger()
    async with mcp_session(app) as session:
        app.state.cost_ledger = ledger
        app.state.crawler = FakeCrawler()
        await ledger.add("anonymous", 2.0)
        estimate = await session.call_tool("deep_research_estimate", {"query": "x"})
        fetched = await session.call_tool("fetch_url", {"urls": ["https://example.com/a"]})

    assert not estimate.is_error
    assert not fetched.is_error


# -- per-key concurrency ----------------------------------------------------


async def test_a_key_cannot_start_more_research_runs_than_the_bound(monkeypatch) -> None:
    """The guard rail the daily cap cannot provide: a loop is stopped up front."""
    monkeypatch.setenv("MCP_RESEARCH_MAX_CONCURRENT", "1")
    get_settings.cache_clear()
    try:
        async with mcp_session(app) as session:
            first = await session.call_tool("deep_research", RESEARCH_ARGS)
            second = await session.call_tool("deep_research", RESEARCH_ARGS)
        assert not first.is_error
        assert second.is_error
        assert "already has 1 deep_research run(s) in flight" in text_of(second)
    finally:
        get_settings.cache_clear()
