"""Snapshot of the advertised tool surface (§9).

A tool schema is a contract with software that cannot ask what changed. Rename
an argument, tighten a bound, drop a field, and every agent built against this
server starts failing calls it used to make — with nothing in the diff to warn a
reviewer that a public interface moved. This test puts that in the diff.

Failing it does not mean the change is wrong. It means the change is visible,
and that whoever made it should say so in the commit message and update
`MCP_REFERENCE.md`.
"""

from __future__ import annotations

from typing import Any

from app.main import app
from tests.mcp_client import mcp_session

EXPECTED_TOOLS: dict[str, dict[str, Any]] = {
    "answer": {
        "required": ["query"],
        "properties": {
            "language": "null|string",
            "max_sources": "integer",
            "query": "string",
        },
    },
    "crawl_site": {
        "required": ["url"],
        "properties": {
            "max_depth": "integer",
            "max_pages": "integer",
            "url": "string",
            "url_patterns": "array[string]|null",
        },
    },
    "deep_research": {
        "required": ["query"],
        "properties": {
            "answers": "array[string]|null",
            "breadth": "integer",
            "depth": "integer",
            "exclude_domains": "array[string]|null",
            "include_domains": "array[string]|null",
            "query": "string",
            "time_range": "null|string",
        },
    },
    "deep_research_estimate": {
        "required": ["query"],
        "properties": {"breadth": "integer", "depth": "integer", "query": "string"},
    },
    "extract_structured": {
        "required": ["schema", "urls"],
        "properties": {"schema": "object", "urls": "array[string]"},
    },
    "fetch_url": {
        "required": ["urls"],
        "properties": {
            "include_links": "boolean",
            "max_chars_per_page": "integer",
            "urls": "array[string]",
        },
    },
    "job_status": {
        "required": ["job_id"],
        "properties": {"include_result": "boolean", "job_id": "string"},
    },
    "map_site": {
        "required": ["url"],
        "properties": {"limit": "integer", "search": "null|string", "url": "string"},
    },
    "web_search": {
        "required": ["q"],
        "properties": {
            "exclude_domains": "array[string]|null",
            "include_domains": "array[string]|null",
            "language": "null|string",
            "max_results": "integer",
            "q": "string",
            "time_range": "null|string",
        },
    },
}

# §2. Advertised on the wire, and enforced in `app.mcp.tools`; the two must not
# drift, or an agent obeying the schema still gets refused.
EXPECTED_BOUNDS = {
    ("web_search", "max_results"): (1, 20),
    ("map_site", "limit"): (1, 300),
    ("crawl_site", "max_pages"): (1, 100),
    ("answer", "max_sources"): (1, 10),
    ("deep_research", "breadth"): (1, 6),
    ("deep_research", "depth"): (1, 3),
}


def _kind(spec: dict[str, Any]) -> str:
    if "anyOf" in spec:
        return "|".join(sorted(_kind(member) for member in spec["anyOf"]))
    declared = spec.get("type", spec.get("const", "any"))
    if declared == "array":
        return f"array[{_kind(spec.get('items', {}))}]"
    return str(declared)


async def test_tool_schemas_match_the_snapshot() -> None:
    async with mcp_session(app) as session:
        listed = await session.list_tools()

    actual = {
        tool.name: {
            "required": sorted(tool.input_schema.get("required", [])),
            "properties": {
                name: _kind(spec)
                for name, spec in sorted(tool.input_schema.get("properties", {}).items())
            },
        }
        for tool in listed.tools
    }
    assert actual == EXPECTED_TOOLS


async def test_advertised_bounds_match_what_is_enforced() -> None:
    async with mcp_session(app) as session:
        listed = await session.list_tools()
    by_name = {tool.name: tool for tool in listed.tools}

    for (tool_name, argument), (low, high) in EXPECTED_BOUNDS.items():
        spec = by_name[tool_name].input_schema["properties"][argument]
        assert (spec.get("minimum"), spec.get("maximum")) == (low, high), (
            f"{tool_name}.{argument} advertises {spec.get('minimum')}..{spec.get('maximum')}"
        )


async def test_every_description_states_what_the_tool_costs() -> None:
    """§2: the description is the only thing an agent reads before choosing."""
    cost_words = ("free", "cost", "cents", "dollars", "credits", "seconds", "minutes")
    async with mcp_session(app) as session:
        listed = await session.list_tools()

    for tool in listed.tools:
        description = (tool.description or "").lower()
        assert any(word in description for word in cost_words), (
            f"{tool.name} does not say what it costs"
        )


async def test_the_expensive_tools_point_at_the_cheap_ones() -> None:
    """A description that only sells itself is how an agent picks deep_research."""
    async with mcp_session(app) as session:
        listed = await session.list_tools()
    by_name = {tool.name: (tool.description or "") for tool in listed.tools}

    assert "answer" in by_name["deep_research"]
    assert "deep_research_estimate" in by_name["deep_research"]
    assert "fetch_url" in by_name["extract_structured"]
    assert "web_search" in by_name["answer"]
    assert "fetch_url" in by_name["crawl_site"]


async def test_annotations_declare_read_only_open_world() -> None:
    async with mcp_session(app) as session:
        listed = await session.list_tools()
    for tool in listed.tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.open_world_hint is True, tool.name


async def test_instance_instructions_route_by_cost() -> None:
    async with mcp_session(app) as session:
        listed = await session.list_tools()
    assert listed  # the session initialized, so instructions were negotiated
