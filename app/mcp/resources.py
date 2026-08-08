"""Resources and prompts (§7).

Resources are the other half of the context budget. `app.mcp.budget` truncates a
tool result because the agent pays for it on every turn; that is only acceptable
because what was cut stays *reachable*. These URIs are where it stays reachable —
pulled on demand, once, by an agent that decided it needed them.

| URI | Contents |
|---|---|
| `search-api://artifact/{id}` | the complete payload of one tool call |
| `search-api://artifact/{id}/page/{n}` | one page of a multi-URL result |
| `search-api://job/{id}` | a job's status and, once finished, its result |
| `search-api://capabilities` | this instance's real limits |

`capabilities` earns its place: it publishes the ceilings the server actually
enforces — URLs per call, token budget, breadth and depth maxima, per-tool
rate-limit weights, remaining daily budget. An agent that can read the
constraints stops guessing them, and stops retrying calls that will always be
refused.

Two prompts, and only because they change what a run costs rather than merely
wording it more nicely:

* `research_brief` turns a vague ask into a properly-shaped research question,
  which is the difference between a $4 report on the wrong topic and a $4 report
  on the right one;
* `verify_claim` checks a statement against sources with the free tools, instead
  of reaching for `answer` out of habit.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server import MCPServer

from app.config import get_settings
from app.mcp.guards import TOOL_WEIGHTS, caller_label
from app.mcp.state import state_of
from app.mcp.tools import (
    MAX_ANSWER_SOURCES,
    MAX_BREADTH,
    MAX_DEPTH,
    MAX_EXTRACT_URLS,
    MAX_MAP_LIMIT,
    MAX_SEARCH_RESULTS,
    MAX_SITE_PAGES,
)

logger = logging.getLogger("unified_search_api.mcp")


def register(server: MCPServer[None], app_state: Any) -> None:
    state = state_of(app_state)

    @server.resource(
        "search-api://artifact/{artifact_id}",
        name="Tool result (full)",
        description="The complete, untruncated payload of a previous tool call.",
        mime_type="text/markdown",
    )
    async def artifact(artifact_id: str) -> str:
        stored = await state.artifacts.get(artifact_id)
        if stored is None:
            return (
                f"No artifact {artifact_id!r}. Artifacts are kept in memory for "
                f"{get_settings().mcp_artifact_ttl / 60:.0f} minutes and then "
                "dropped; run the tool again to regenerate it."
            )
        parts: list[str] = []
        for index, page in enumerate(stored.pages, start=1):
            header = f"## [{index}] {page.title or page.meta.get('url') or ''}".rstrip()
            error = page.meta.get("error")
            body = page.text or (f"(no content: {error})" if error else "(no content)")
            parts.append(f"{header}\n\n{body}")
        if not parts and stored.payload:
            return json.dumps(stored.payload, indent=2, ensure_ascii=False)
        return "\n\n".join(parts)

    @server.resource(
        "search-api://artifact/{artifact_id}/page/{page_number}",
        name="Tool result (one page)",
        description="One page of a multi-URL tool result, in full.",
        mime_type="text/markdown",
    )
    async def artifact_page(artifact_id: str, page_number: str) -> str:
        stored = await state.artifacts.get(artifact_id)
        if stored is None:
            return f"No artifact {artifact_id!r} (expired or never existed)."
        try:
            index = int(page_number)
        except ValueError:
            return f"{page_number!r} is not a page number."
        if not 1 <= index <= len(stored.pages):
            return (
                f"Artifact {artifact_id} has {len(stored.pages)} page(s); there is no page {index}."
            )
        page = stored.pages[index - 1]
        title = page.title or page.meta.get("url") or f"Page {index}"
        return f"# {title}\n\n{page.text or '(no content)'}"

    @server.resource(
        "search-api://job/{job_id}",
        name="Job",
        description="Status and, once it has finished, the result of a background job.",
        mime_type="application/json",
    )
    async def job(job_id: str) -> str:
        found = state.jobs.get(job_id)
        if found is None:
            return json.dumps({"job_id": job_id, "status": "unknown"}, indent=2)
        return json.dumps(found.public(), indent=2, ensure_ascii=False, default=str)

    @server.resource(
        "search-api://capabilities",
        name="Instance capabilities",
        description="The limits this instance actually enforces, and what each tool costs.",
        mime_type="application/json",
    )
    async def capabilities() -> str:
        settings = get_settings()
        label = caller_label()
        cap = settings.daily_cost_cap_usd
        spent = await state.cost_ledger.spent_today(label)
        payload: dict[str, Any] = {
            "instance": {
                "auth_enabled": settings.auth_enabled,
                "default_model": settings.llm_model,
                "report_model": settings.report_model,
                "documents_enabled": settings.documents_enabled,
            },
            "context_budget": {
                "max_result_tokens": settings.mcp_max_result_tokens,
                "note": (
                    "Every tool result is capped at max_result_tokens. Truncated "
                    "text is marked in place and readable at search-api://artifact/{id}."
                ),
                "artifact_ttl_seconds": settings.mcp_artifact_ttl,
                "image_mode": settings.mcp_image_mode,
            },
            "limits": {
                "web_search.max_results": MAX_SEARCH_RESULTS,
                "map_site.limit": MAX_MAP_LIMIT,
                "fetch_url.urls": settings.mcp_max_urls_per_call,
                "crawl_site.max_pages": MAX_SITE_PAGES,
                "answer.max_sources": MAX_ANSWER_SOURCES,
                "extract_structured.urls": MAX_EXTRACT_URLS,
                "deep_research.breadth": MAX_BREADTH,
                "deep_research.depth": MAX_DEPTH,
                "deep_research.max_concurrent_per_key": settings.mcp_research_max_concurrent,
            },
            "rate_limit": {
                "enabled": settings.rate_limit_enabled,
                "points_per_60s": settings.rate_limit_overrides.get(label, settings.rate_limit_rpm),
                "tool_costs": dict(sorted(TOOL_WEIGHTS.items(), key=lambda kv: kv[1])),
            },
            "daily_cost_cap_usd": {
                "enabled": cap > 0,
                "cap": cap,
                "spent_today": round(spent, 6),
                "remaining": round(max(cap - spent, 0.0), 6) if cap > 0 else None,
            },
        }
        return json.dumps(payload, indent=2)

    @server.prompt(
        name="research_brief",
        title="Turn a vague ask into a research brief",
        description=(
            "Shape a loose question into one deep_research can actually answer, "
            "with a breadth/depth suggestion — before spending dollars on it."
        ),
    )
    def research_brief(topic: str) -> str:
        return (
            f"I want to research: {topic}\n\n"
            "Before running deep_research, help me turn this into a brief:\n"
            "1. Restate it as a single, specific research question. Name the "
            "scope: what time period, which geography, which kind of source "
            "counts as authoritative here?\n"
            "2. List what I am actually trying to decide or find out, so the "
            "report can be judged against it.\n"
            "3. Suggest breadth (search queries per level, 1-6) and depth "
            "(recursion levels, 1-3), and say why. Narrow, factual questions want "
            "low numbers; contested or fast-moving topics want more breadth.\n"
            "4. Name domains worth excluding — content farms, SEO aggregators, "
            "anything that will pad the sources without adding evidence.\n"
            "5. Run deep_research_estimate on your proposed shape and tell me "
            "what it would cost before we commit."
        )

    @server.prompt(
        name="verify_claim",
        title="Check a claim against sources",
        description=(
            "Verify a statement using web_search and fetch_url — the free tools — "
            "rather than asking a model to vouch for it."
        ),
    )
    def verify_claim(claim: str) -> str:
        return (
            f"Verify this claim: {claim}\n\n"
            "Work from sources, not from memory:\n"
            "1. Use web_search to find pages that would confirm or refute it. "
            "Search for the refutation too, not only the confirmation.\n"
            "2. Use fetch_url to read the most credible three or four — primary "
            "sources, official documentation and original reporting over "
            "summaries of summaries.\n"
            "3. Quote the exact sentences that bear on the claim, with their URL.\n"
            "4. Rule: supported / contradicted / unverifiable from these sources. "
            "If the sources disagree, say so and show both.\n"
            "5. Note anything that would change the verdict — a date, a "
            "jurisdiction, a version number.\n\n"
            "Do not use `answer` for this: it would hand you a model's synthesis "
            "where the point is to read the sources yourself."
        )
