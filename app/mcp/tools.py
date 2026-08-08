"""The nine MCP tools (§2).

Not a 1:1 wrapper over the nine HTTP endpoints, and deliberately not. A wrapper
would be a mediocre MCP server for reasons that have nothing to do with the
protocol:

1. **Selection.** `search`, `answer` and `deep_research` all answer "find me
   things about X", three orders of magnitude apart in cost. The only place an
   agent looks before choosing is the description, so every description here
   states the cost in words and names the tool to use *instead* in the cases it
   is wrong for.
2. **Context.** A result is not an HTTP response; it is billed to the agent's
   window on every following turn. `app.mcp.budget` caps it.
3. **Unusable knobs.** `prune_threshold`, `word_count_threshold`,
   `content_filter`, `include_raw_markdown`, `exclude_external_links` — nobody
   can set these blind, and an agent will fill them with plausible noise. They
   are instance settings, so they stay instance settings.
4. **Long runs.** A tool call that blocks for eight minutes is a tool call that
   times out. `crawl_site` and `deep_research` start jobs (see `app.mcp.tasks`).

Every ceiling here is *lower* than its HTTP equivalent — 10 URLs instead of 50,
breadth 6 instead of 10 — for the same reason: the HTTP caps bound a server's
work, these bound an autonomous loop's spending.

Annotations are set honestly: everything is `read_only_hint=True` (nothing here
mutates anything the server owns) and `open_world_hint=True` (all of it reaches
the live web).

.. note::
   This module deliberately omits ``from __future__ import annotations``. Tool
   schemas are built by introspecting these signatures, and several bounds come
   from configuration read at startup (`MCP_MAX_URLS_PER_CALL`). Deferred string
   annotations cannot close over a local, so `tools/list` would advertise a
   constant where the instance enforces a setting — and an agent that cannot
   trust the advertised limit retries calls the server will always refuse.
"""

import json
import logging
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import (
    CallToolResult,
    ContentBlock,
    ImageContent,
    TextContent,
    ToolAnnotations,
)
from pydantic import AnyHttpUrl, Field

from app.api.deps import record_usage
from app.config import Settings, get_settings
from app.core import metrics
from app.core.llm.usage import UsageTracker
from app.core.net.guard import BlockedUrlError
from app.core.research.engine import DeepResearchEngine, query_looks_specific
from app.core.research.estimate import estimate_run
from app.core.search.searxng import SearxngError
from app.jobs.manager import Emit
from app.mcp import tasks
from app.mcp.artifacts import ArtifactPage
from app.mcp.budget import budget_pages, job_uri
from app.mcp.elicit import resolve_clarification
from app.mcp.guards import ToolRefused, caller_label, check_cost_cap, tool_call
from app.mcp.state import McpState, state_of
from app.models.answer import AnswerRequest
from app.models.crawl import CrawlFormat, CrawlRequest, SiteCrawlRequest
from app.models.extract import ExtractRequest
from app.models.map import MapRequest
from app.models.search import SearchRequest, TimeRange
from app.services.answer import AnswerService
from app.services.extract import ExtractService
from app.services.mapper import MapService

logger = logging.getLogger("unified_search_api.mcp")

# MCP-side ceilings (§2). Lower than the HTTP models' own caps on purpose.
MAX_SEARCH_RESULTS = 20
MAX_MAP_LIMIT = 300
MAX_SITE_PAGES = 100
MAX_SITE_DEPTH = 4
MAX_ANSWER_SOURCES = 10
MAX_EXTRACT_URLS = 5
MAX_BREADTH = 6
MAX_DEPTH = 3

_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_READ_ONLY_IDEMPOTENT = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=True
)


def _ok(blocks: list[ContentBlock], structured: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=blocks, structured_content=structured)


def _text_only(text: str, structured: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)], structured_content=structured
    )


def _clean_urls(urls: list[str], limit: int) -> list[AnyHttpUrl]:
    """Validate and de-duplicate a URL list, refusing above ``limit``.

    Refused rather than silently truncated: an agent that asked for twelve URLs
    and got eight will assume the other four had no content.
    """
    seen: list[str] = []
    for raw in urls:
        candidate = raw.strip()
        if candidate and candidate not in seen:
            seen.append(candidate)
    if not seen:
        raise ToolRefused("No URL given.")
    if len(seen) > limit:
        raise ToolRefused(
            f"{len(seen)} URLs given, but this instance accepts at most {limit} per "
            f"call. Split the list across several calls, or crawl the site with "
            "crawl_site if you want everything under one address."
        )
    try:
        return [AnyHttpUrl(url) for url in seen]
    except Exception as exc:
        raise ToolRefused(f"Not a usable http(s) URL list: {exc}") from exc


def register(server: MCPServer[None], app_state: Any) -> None:
    """Define the nine tools over the clients already on ``app.state``."""
    state = state_of(app_state)
    startup_settings = get_settings()
    max_urls = max(startup_settings.mcp_max_urls_per_call, 1)
    default_page_chars = 8000

    # ---------------------------------------------------------------- tier 0
    # Free and instant. The tools an agent should reach for first, and the ones
    # the expensive descriptions point back at.

    @server.tool(
        name="web_search",
        title="Web search",
        annotations=_READ_ONLY_IDEMPOTENT,
        structured_output=False,
        description=(
            "Search the web. Returns titles, URLs and snippets — NOT page content. "
            "Free and instant.\n\n"
            "This is the tool for finding out *which* pages are worth reading; "
            "follow up with fetch_url to actually read them. Do not use answer or "
            "deep_research when a list of links is what you want — they cost money "
            "and they read pages you may not need."
        ),
    )
    async def web_search(
        ctx: Context[Any, Any],
        q: Annotated[str, Field(min_length=1, description="Search query.")],
        max_results: Annotated[
            int, Field(default=8, ge=1, le=MAX_SEARCH_RESULTS, description="Results to return.")
        ] = 8,
        time_range: Annotated[
            TimeRange | None,
            Field(default=None, description="Restrict to the last day/week/month/year."),
        ] = None,
        include_domains: Annotated[
            list[str] | None,
            Field(default=None, description="Keep only these domains (subdomains included)."),
        ] = None,
        exclude_domains: Annotated[
            list[str] | None,
            Field(default=None, description="Drop these domains (subdomains included)."),
        ] = None,
        language: Annotated[
            str | None, Field(default=None, description="Language hint, e.g. 'en', 'fr'.")
        ] = None,
    ) -> CallToolResult:
        settings = get_settings()
        async with tool_call(ctx, "web_search", settings) as call:
            request = SearchRequest(
                q=q,
                max_results=min(max_results, MAX_SEARCH_RESULTS),
                time_range=time_range,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                language=language,
            )
            try:
                response = await state.searxng.search(request)
            except SearxngError as exc:
                raise ToolRefused(f"Search backend unavailable: {exc}") from exc

            lines = [
                f"{index}. {item.title}\n   {item.url}\n   {item.snippet}".rstrip()
                for index, item in enumerate(response.results, start=1)
            ]
            body = "\n\n".join(lines) or "No results."
            budgeted = await budget_pages(
                tool="web_search",
                pages=[ArtifactPage(title=f"Results for {q!r}", text=body)],
                store=state.artifacts,
                max_result_tokens=settings.mcp_max_result_tokens,
            )
            call.result_tokens = budgeted.text_tokens
            return _ok(
                budgeted.blocks,
                {
                    "query": response.query,
                    "count": len(response.results),
                    "results": [item.model_dump() for item in response.results],
                },
            )

    @server.tool(
        name="map_site",
        title="List a site's URLs",
        annotations=_READ_ONLY_IDEMPOTENT,
        structured_output=False,
        description=(
            "List the URLs of a site, from its sitemap.xml, its robots.txt, or "
            "failing both, the links on the page you gave. Free — one HTTP "
            "request, no browser, no model.\n\n"
            "Run this BEFORE crawl_site: it tells you how big the site is and "
            "which sections exist, so you crawl the twenty pages you want instead "
            "of the four hundred you don't."
        ),
    )
    async def map_site(
        ctx: Context[Any, Any],
        url: Annotated[
            str, Field(description="Any URL on the site; its origin is the entry point.")
        ],
        search: Annotated[
            str | None,
            Field(default=None, description="Keep only URLs containing this substring."),
        ] = None,
        limit: Annotated[
            int, Field(default=100, ge=1, le=MAX_MAP_LIMIT, description="Max URLs returned.")
        ] = 100,
    ) -> CallToolResult:
        settings = get_settings()
        async with tool_call(ctx, "map_site", settings) as call:
            service = MapService(
                crawler=state.crawler, guard=state.url_guard, timeout=settings.map_timeout
            )
            request = MapRequest(
                url=_clean_urls([url], 1)[0], search=search, limit=min(limit, MAX_MAP_LIMIT)
            )
            try:
                response = await service.map_site(request)
            except BlockedUrlError as exc:
                raise ToolRefused(f"URL not allowed: {exc}") from exc
            except Exception as exc:
                raise ToolRefused(f"Could not map the site: {exc}") from exc

            budgeted = await budget_pages(
                tool="map_site",
                pages=[
                    ArtifactPage(
                        title=f"{response.count} URLs (source: {response.source})",
                        text="\n".join(response.links) or "No URLs found.",
                    )
                ],
                store=state.artifacts,
                max_result_tokens=settings.mcp_max_result_tokens,
            )
            call.result_tokens = budgeted.text_tokens
            return _ok(budgeted.blocks, response.model_dump())

    # ---------------------------------------------------------------- tier 1
    # Costs browser time, not model credits.

    async def _fetch(
        ctx: Context[Any, Any],
        urls: list[str],
        max_chars_per_page: int,
        include_links: bool,
        capture: str,
    ) -> CallToolResult:
        settings = get_settings()
        async with tool_call(ctx, "fetch_url", settings) as call:
            mode = settings.mcp_image_mode
            if capture != "none" and mode == "off":
                raise ToolRefused(
                    f"{capture} capture is disabled on this instance "
                    "(MCP_IMAGE_MODE=off). Fetch the page as text instead."
                )
            formats: list[CrawlFormat] = ["markdown"]
            if include_links:
                formats.append("links")
            if capture in ("screenshot", "pdf"):
                formats.append(capture)  # type: ignore[arg-type]

            request = CrawlRequest(
                urls=_clean_urls(urls, min(max_urls, 25)),
                formats=formats,
                ignore_links=not include_links,
            )
            try:
                pages = await state.crawler.crawl(request)
            except BlockedUrlError as exc:
                raise ToolRefused(f"URL not allowed: {exc}") from exc
            except Exception as exc:
                raise ToolRefused(f"Fetch failed: {exc}") from exc
            metrics.record_crawled_pages(pages)

            artifact_pages = [
                ArtifactPage(
                    title=page.title or page.url,
                    text=page.markdown or "",
                    meta={
                        "url": page.url,
                        "success": page.success,
                        "status_code": page.status_code,
                        "word_count": page.word_count,
                        "source_format": page.source_format,
                        "error": page.error,
                    },
                )
                for page in pages
            ]
            budgeted = await budget_pages(
                tool="fetch_url",
                pages=artifact_pages,
                store=state.artifacts,
                max_result_tokens=settings.mcp_max_result_tokens,
                max_chars_per_page=max_chars_per_page,
                payload={"count": len(pages)},
            )
            call.result_tokens = budgeted.text_tokens
            blocks = list(budgeted.blocks)
            if capture != "none":
                blocks.extend(await _capture_blocks(pages, capture, mode, settings))
            return _ok(
                blocks,
                {
                    "count": len(pages),
                    "pages": [
                        {
                            "url": page.url,
                            "success": page.success,
                            "status_code": page.status_code,
                            "title": page.title,
                            "word_count": page.word_count,
                            "source_format": page.source_format,
                            "error": page.error,
                            "links": page.links if include_links else None,
                        }
                        for page in pages
                    ],
                },
            )

    async def _capture_blocks(
        pages: list[Any], capture: str, mode: str, settings: Settings
    ) -> list[ContentBlock]:
        """Screenshots and PDF renders, as links or as inline images (§3).

        Never inline by default: a base64 PNG is tens of thousands of tokens that
        the agent re-sends on every turn, for a picture it usually cannot read.
        """
        blocks: list[ContentBlock] = []
        for page in pages:
            data = getattr(page, capture, None)
            if not data:
                continue
            mime = "image/png" if capture == "screenshot" else "application/pdf"
            if mode == "inline" and capture == "screenshot":
                blocks.append(ImageContent(type="image", data=data, mime_type=mime))
                continue
            artifact = await state.artifacts.put(
                f"fetch_url:{capture}",
                [ArtifactPage(title=page.url, text="", meta={"url": page.url})],
                payload={"encoding": "base64", "mime_type": mime, "data": data},
            )
            from mcp.types import ResourceLink

            from app.mcp.budget import artifact_uri

            blocks.append(
                ResourceLink(
                    type="resource_link",
                    uri=artifact_uri(artifact.id),
                    name=f"{capture} of {page.url}",
                    description=f"Base64 {mime}. Load it only if you can render it.",
                    mime_type=mime,
                )
            )
        return blocks

    _FETCH_DESCRIPTION = (
        "Read web pages and get their content back as clean Markdown. A few "
        "seconds per page, no model call, no credits spent.\n\n"
        f"Takes up to {max_urls} URLs at once. Long pages are truncated to fit "
        "your context; the cut is marked in place and the full text stays "
        "readable as a search-api://artifact/... resource. Handles PDFs, Word, "
        "Excel and PowerPoint documents as well as HTML.\n\n"
        "If you do not know which URLs to read yet, run web_search first."
    )

    if startup_settings.mcp_image_mode == "off":

        @server.tool(
            name="fetch_url",
            title="Read web pages",
            annotations=_READ_ONLY,
            structured_output=False,
            description=_FETCH_DESCRIPTION,
        )
        async def fetch_url(
            ctx: Context[Any, Any],
            urls: Annotated[
                list[str],
                Field(min_length=1, max_length=max_urls, description="Pages to read."),
            ],
            max_chars_per_page: Annotated[
                int,
                Field(
                    default=default_page_chars,
                    ge=200,
                    le=100_000,
                    description=(
                        "Characters kept per page. The instance-wide token budget "
                        "still applies and may cut further."
                    ),
                ),
            ] = default_page_chars,
            include_links: Annotated[
                bool,
                Field(default=False, description="Also return the page's outgoing links."),
            ] = False,
        ) -> CallToolResult:
            return await _fetch(ctx, urls, max_chars_per_page, include_links, "none")

    else:

        @server.tool(
            name="fetch_url",
            title="Read web pages",
            annotations=_READ_ONLY,
            structured_output=False,
            description=(
                _FETCH_DESCRIPTION + "\n\nThis instance can also capture a "
                "screenshot or a PDF render of a page; both are returned as "
                "resources rather than inline data unless the operator enabled "
                "inline images."
            ),
        )
        async def fetch_url_with_capture(
            ctx: Context[Any, Any],
            urls: Annotated[
                list[str],
                Field(min_length=1, max_length=max_urls, description="Pages to read."),
            ],
            max_chars_per_page: Annotated[
                int, Field(default=default_page_chars, ge=200, le=100_000)
            ] = default_page_chars,
            include_links: Annotated[bool, Field(default=False)] = False,
            capture: Annotated[
                str,
                Field(
                    default="none",
                    pattern="^(none|screenshot|pdf)$",
                    description="Also render the page as an image or a PDF.",
                ),
            ] = "none",
        ) -> CallToolResult:
            return await _fetch(ctx, urls, max_chars_per_page, include_links, capture)

    @server.tool(
        name="crawl_site",
        title="Crawl a whole site (long-running)",
        annotations=_READ_ONLY,
        structured_output=False,
        description=(
            "Crawl a whole site by following its links. MINUTES, not seconds — so "
            "it starts a background job and returns a job id, not the pages. Poll "
            "job_status to collect the result.\n\n"
            "For a handful of URLs you already know, fetch_url is far faster and "
            "returns immediately. Run map_site first to see what you are about to "
            "set off."
        ),
    )
    async def crawl_site(
        ctx: Context[Any, Any],
        url: Annotated[str, Field(description="Entry point to crawl from.")],
        max_pages: Annotated[
            int, Field(default=25, ge=1, le=MAX_SITE_PAGES, description="Hard cap on pages.")
        ] = 25,
        max_depth: Annotated[
            int, Field(default=2, ge=0, le=MAX_SITE_DEPTH, description="Link-following depth.")
        ] = 2,
        url_patterns: Annotated[
            list[str] | None,
            Field(
                default=None, description="Only crawl URLs matching these globs, e.g. '*/docs/*'."
            ),
        ] = None,
    ) -> CallToolResult:
        settings = get_settings()
        async with tool_call(ctx, "crawl_site", settings) as call:
            entry = _clean_urls([url], 1)[0]
            try:
                await state.url_guard.check(str(entry))
            except BlockedUrlError as exc:
                raise ToolRefused(f"URL not allowed: {exc}") from exc

            request = SiteCrawlRequest(
                url=entry,
                max_pages=min(max_pages, MAX_SITE_PAGES),
                max_depth=min(max_depth, MAX_SITE_DEPTH),
                url_patterns=url_patterns,
            )

            async def task(emit: Emit) -> dict[str, Any]:
                pages = await state.crawler.crawl_site(request, emit=emit)
                metrics.record_crawled_pages(pages)
                return {
                    "base_url": str(request.url),
                    "count": len(pages),
                    "pages": [page.model_dump() for page in pages],
                }

            job = await tasks.launch(state.jobs, "site_crawl", task)
            call.result_tokens = 0
            return _text_only(
                tasks.submitted_text(job, tool="crawl_site"),
                tasks.submitted_payload(job, tool="crawl_site"),
            )

    # ---------------------------------------------------------------- tier 2
    # Spends LLM credits.

    @server.tool(
        name="answer",
        title="Sourced answer",
        annotations=_READ_ONLY,
        structured_output=False,
        description=(
            "Answer a question from the web in one pass: search, read the top "
            "pages, one LLM call. A few seconds and a few cents. Returns the "
            "answer with numbered sources.\n\n"
            "Use it for a factual question whose answer fits in a paragraph or "
            "two. For a cited report on a broad topic, use deep_research — but "
            "read its cost note first. If you only want links, web_search is free."
        ),
    )
    async def answer(
        ctx: Context[Any, Any],
        query: Annotated[str, Field(min_length=1, description="The question to answer.")],
        max_sources: Annotated[
            int,
            Field(default=5, ge=1, le=MAX_ANSWER_SOURCES, description="Pages to consult."),
        ] = 5,
        language: Annotated[
            str | None, Field(default=None, description="Search language hint, e.g. 'fr'.")
        ] = None,
    ) -> CallToolResult:
        settings = get_settings()
        async with tool_call(ctx, "answer", settings) as call:
            label = caller_label()
            await check_cost_cap(state.cost_ledger, label, settings)
            service = AnswerService(
                searxng=state.searxng,
                crawler=state.crawler,
                llm=state.llm,
                cache=state.answer_cache,
            )
            usage = UsageTracker()
            request = AnswerRequest(
                query=query,
                max_sources=min(max_sources, MAX_ANSWER_SOURCES),
                language=language,
            )
            try:
                response = await service.answer(request, usage)
            except Exception as exc:
                # Billed even on failure, exactly as POST /v1/answer does: the
                # provider charged for whatever it produced before it broke.
                await record_usage(state.cost_ledger, label, usage)
                raise ToolRefused(f"Answer generation failed: {exc}") from exc
            await record_usage(state.cost_ledger, label, usage)

            sources = "\n".join(f"[{s.index}] {s.title} — {s.url}" for s in response.sources)
            budgeted = await budget_pages(
                tool="answer",
                pages=[
                    ArtifactPage(
                        title=f"Answer ({response.model})",
                        text=f"{response.answer}\n\n### Sources\n{sources}",
                    )
                ],
                store=state.artifacts,
                max_result_tokens=settings.mcp_max_result_tokens,
            )
            call.result_tokens = budgeted.text_tokens
            return _ok(budgeted.blocks, response.model_dump())

    @server.tool(
        name="extract_structured",
        title="Extract typed fields from pages",
        annotations=_READ_ONLY,
        structured_output=False,
        description=(
            "Pull typed fields out of web pages according to a JSON Schema you "
            "supply. ONE LLM CALL PER URL, so it costs roughly its number of "
            "URLs in model calls.\n\n"
            f"Up to {MAX_EXTRACT_URLS} URLs per call. If you just want the text of "
            "the pages, fetch_url does that for free and you can read it yourself."
        ),
    )
    async def extract_structured(
        ctx: Context[Any, Any],
        urls: Annotated[
            list[str],
            Field(min_length=1, max_length=MAX_EXTRACT_URLS, description="Pages to extract from."),
        ],
        schema: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "JSON Schema object describing the fields to extract, e.g. "
                    '{"type":"object","properties":{"price_usd":{"type":"number"}}}.'
                )
            ),
        ],
    ) -> CallToolResult:
        settings = get_settings()
        async with tool_call(ctx, "extract_structured", settings) as call:
            label = caller_label()
            await check_cost_cap(state.cost_ledger, label, settings)
            request = ExtractRequest.model_validate(
                {
                    "urls": [str(url) for url in _clean_urls(urls, MAX_EXTRACT_URLS)],
                    # `mode` is not exposed: the "css" mode wants a crawl4ai
                    # selector schema, which nobody can write for a page they
                    # have not seen. "llm" is the only generic one.
                    "mode": "llm",
                    # The field is `json_schema` internally and `schema` on the
                    # wire; validating a dict keeps the alias the single spelling.
                    "schema": schema,
                }
            )
            service = ExtractService(crawler=state.crawler, llm=state.llm)
            try:
                results = await service.extract(request)
            except BlockedUrlError as exc:
                raise ToolRefused(f"URL not allowed: {exc}") from exc
            except ValueError as exc:
                raise ToolRefused(f"Unusable JSON Schema: {exc}") from exc
            except Exception as exc:
                raise ToolRefused(f"Extraction failed: {exc}") from exc

            pages = [
                ArtifactPage(
                    title=result.url,
                    text=json.dumps(result.data, indent=2, ensure_ascii=False)
                    if result.data
                    else "",
                    meta={"url": result.url, "error": result.error},
                )
                for result in results
            ]
            budgeted = await budget_pages(
                tool="extract_structured",
                pages=pages,
                store=state.artifacts,
                max_result_tokens=settings.mcp_max_result_tokens,
            )
            call.result_tokens = budgeted.text_tokens
            return _ok(
                budgeted.blocks,
                {"count": len(results), "results": [r.model_dump() for r in results]},
            )

    # ---------------------------------------------------------------- tier 3
    # Spends seriously.

    @server.tool(
        name="deep_research_estimate",
        title="Price a deep_research run",
        annotations=_READ_ONLY_IDEMPOTENT,
        structured_output=False,
        description=(
            "Price a deep_research run WITHOUT starting it: pages it would read, "
            "model calls it would make, projected cost in dollars. Free and "
            "instant — no search, no crawl, no model call.\n\n"
            "Call this before deep_research whenever the topic is broad or you "
            "are unsure the report is worth it."
        ),
    )
    async def deep_research_estimate(
        ctx: Context[Any, Any],
        query: Annotated[str, Field(min_length=1, description="The research question.")],
        breadth: Annotated[
            int, Field(default=4, ge=1, le=MAX_BREADTH, description="Search queries per level.")
        ] = 4,
        depth: Annotated[
            int, Field(default=2, ge=1, le=MAX_DEPTH, description="Recursion levels.")
        ] = 2,
    ) -> CallToolResult:
        settings = get_settings()
        async with tool_call(ctx, "deep_research_estimate", settings) as call:
            estimate = estimate_run(
                breadth=min(breadth, MAX_BREADTH),
                depth=min(depth, MAX_DEPTH),
                model=settings.report_model or settings.llm_model,
                crawl_top_k=settings.research_crawl_top_k,
                process_context_token_budget=settings.research_process_context_token_budget,
                report_token_budget=settings.research_report_token_budget,
                rerank_enabled=settings.research_rerank == "llm",
            )
            text = (
                f"deep_research(query={query!r}, breadth={breadth}, depth={depth}) would:\n"
                f"- run {estimate['serp_queries']} search queries\n"
                f"- read {estimate['pages_crawled']} pages\n"
                f"- make {estimate['llm_calls']} model calls on {estimate['model']}\n"
                f"- cost about ${estimate['estimated_cost_usd']:.4f}\n\n"
                "The figure is accurate to roughly a factor of two — enough to "
                "answer 'cents or dollars?', not an invoice."
            )
            call.result_tokens = 0
            return _text_only(text, {"query": query, "estimate": dict(estimate)})

    @server.tool(
        name="deep_research",
        title="Deep research report (long-running)",
        annotations=_READ_ONLY,
        structured_output=False,
        description=(
            "Produce a cited research report: recursive searches, dozens of pages "
            "read, a written report with numbered sources. SEVERAL MINUTES AND "
            "SEVERAL DOLLARS PER CALL.\n\n"
            "It starts a background job and returns a job id; poll job_status for "
            "the report. It may first come back with clarifying questions — answer "
            "them by calling it again with `answers`.\n\n"
            "Do NOT call this for a question with a short answer: use answer, "
            "which costs cents and replies in seconds. Price the run first with "
            "deep_research_estimate if you are unsure."
        ),
    )
    async def deep_research(
        ctx: Context[Any, Any],
        query: Annotated[str, Field(min_length=1, description="The research question.")],
        breadth: Annotated[
            int, Field(default=4, ge=1, le=MAX_BREADTH, description="Search queries per level.")
        ] = 4,
        depth: Annotated[
            int, Field(default=2, ge=1, le=MAX_DEPTH, description="Recursion levels.")
        ] = 2,
        answers: Annotated[
            list[str] | None,
            Field(
                default=None,
                description="Answers to clarifying questions a previous call returned.",
            ),
        ] = None,
        time_range: Annotated[
            TimeRange | None,
            Field(default=None, description="Restrict sources to the last day/week/month/year."),
        ] = None,
        include_domains: Annotated[
            list[str] | None, Field(default=None, description="Only research these domains.")
        ] = None,
        exclude_domains: Annotated[
            list[str] | None, Field(default=None, description="Never research these domains.")
        ] = None,
    ) -> CallToolResult:
        settings = get_settings()
        async with tool_call(ctx, "deep_research", settings) as call:
            label = caller_label()
            await check_cost_cap(state.cost_ledger, label, settings)
            call.result_tokens = 0

            engine = _engine(state, settings)
            usage = UsageTracker()
            given = [a for a in (answers or []) if a.strip()]

            # Clarification (§5). Skipped when the caller already answered, and
            # when the query is specific enough that the heuristic is sure — that
            # branch costs nothing, where asking a model whether to ask is itself
            # a model call.
            if not given and not query_looks_specific(query):
                try:
                    questions = await engine.generate_feedback(query, usage=usage)
                except Exception as exc:
                    await record_usage(state.cost_ledger, label, usage)
                    raise ToolRefused(f"Clarification step failed: {exc}") from exc
                if questions:
                    elicited = await resolve_clarification(ctx, query, questions)
                    if elicited is not None:
                        given = elicited
                    else:
                        await record_usage(state.cost_ledger, label, usage)
                        listed = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
                        return _text_only(
                            "Before spending several dollars on this, the research "
                            f"engine needs a few things pinned down:\n\n{listed}\n\n"
                            "Call deep_research again with the same query and an "
                            "`answers` array holding one answer per question, in "
                            "order. To research the question as-is without "
                            'answering, pass answers=["as stated"].',
                            {
                                "status": "needs_clarification",
                                "query": query,
                                "questions": questions,
                                "usage": usage.snapshot(),
                            },
                        )

            effective_query = query
            if given:
                joined = "\n".join(f"- {a}" for a in given)
                effective_query = f"{query}\n\nAdditional context from the user:\n{joined}"

            # Acquired before the job exists and released by the job itself: the
            # run outlives this call by minutes (see ConcurrencyBound).
            state.research_bound.acquire(label, "deep_research")

            async def task(emit: Emit) -> dict[str, Any]:
                try:
                    result = await engine.run(
                        effective_query,
                        breadth=min(breadth, MAX_BREADTH),
                        depth=min(depth, MAX_DEPTH),
                        on_progress=tasks.research_emitter(emit),
                        time_range=time_range,
                        include_domains=include_domains,
                        exclude_domains=exclude_domains,
                        usage=usage,
                    )
                except BaseException:
                    await record_usage(state.cost_ledger, label, usage)
                    raise
                finally:
                    state.research_bound.release(label)
                await record_usage(state.cost_ledger, label, usage)
                return {
                    "query": query,
                    "report": result.report,
                    "learnings": [item.text for item in result.learnings],
                    "sources": result.sources,
                    "citations": [c.model_dump() for c in result.citations],
                    "serp_queries": result.serp_queries,
                    "stopped_early": result.stopped_early,
                    "usage": result.usage.snapshot(),
                }

            try:
                job = await tasks.launch(state.jobs, "research", task)
            except BaseException:
                state.research_bound.release(label)
                raise

            estimate = estimate_run(
                breadth=min(breadth, MAX_BREADTH),
                depth=min(depth, MAX_DEPTH),
                model=settings.report_model or settings.llm_model,
                crawl_top_k=settings.research_crawl_top_k,
                process_context_token_budget=settings.research_process_context_token_budget,
                report_token_budget=settings.research_report_token_budget,
                rerank_enabled=settings.research_rerank == "llm",
            )
            note = (
                f"Projected cost of this run: about "
                f"${estimate['estimated_cost_usd']:.4f} "
                f"({estimate['pages_crawled']} pages, {estimate['llm_calls']} model calls)."
            )
            payload = tasks.submitted_payload(job, tool="deep_research", note=note)
            payload["usage"] = {"estimated_cost_usd": estimate["estimated_cost_usd"]}
            if given:
                payload["answers_used"] = given
            return _text_only(tasks.submitted_text(job, tool="deep_research", note=note), payload)

    # ------------------------------------------------------------ job status

    @server.tool(
        name="job_status",
        title="Collect a long-running job",
        annotations=_READ_ONLY_IDEMPOTENT,
        structured_output=False,
        description=(
            "Check on — and collect the result of — a job started by "
            "deep_research or crawl_site. Free.\n\n"
            "While the job is running this returns its status and the last few "
            "progress steps; once it finishes, the result. Wait between polls "
            "(the job tells you how long): polling in a tight loop spends "
            "rate-limit budget and learns nothing."
        ),
    )
    async def job_status(
        ctx: Context[Any, Any],
        job_id: Annotated[str, Field(description="Id returned by deep_research or crawl_site.")],
        include_result: Annotated[
            bool,
            Field(default=True, description="Return the result, not just the status."),
        ] = True,
    ) -> CallToolResult:
        settings = get_settings()
        async with tool_call(ctx, "job_status", settings) as call:
            job = state.jobs.get(job_id)
            if job is None:
                raise ToolRefused(
                    f"No job {job_id!r}. Either it never existed, or it finished "
                    "long enough ago to have been evicted — results are kept for "
                    f"{settings.job_retention_seconds / 60:.0f} minutes."
                )
            public = job.public()
            status = public["status"]
            if status in ("pending", "running"):
                trail = tasks.progress_tail(job)
                steps = tasks.progress_summary(trail)
                poll = tasks.POLL_SECONDS.get(job.type, tasks.DEFAULT_POLL_SECONDS)
                call.result_tokens = 0
                return _text_only(
                    f"Job {job_id} ({job.type}) is {status}.\n\nLatest steps:\n{steps}\n\n"
                    f"Call job_status again in about {poll}s.",
                    {
                        "job_id": job_id,
                        "type": job.type,
                        "status": status,
                        "progress": trail,
                        "wait_seconds_between_polls": poll,
                    },
                )
            if status == "failed":
                call.result_tokens = 0
                return _text_only(
                    f"Job {job_id} ({job.type}) failed: {public['error']}",
                    {
                        "job_id": job_id,
                        "type": job.type,
                        "status": status,
                        "error": public["error"],
                    },
                )

            result = public["result"] or {}
            if not include_result:
                call.result_tokens = 0
                return _text_only(
                    f"Job {job_id} ({job.type}) completed. Call job_status with "
                    "include_result=true to read it, or open the resource "
                    f"{job_uri(job_id)}.",
                    {"job_id": job_id, "type": job.type, "status": status},
                )

            pages, structured = _job_result_pages(job.type, job_id, result)
            budgeted = await budget_pages(
                tool="job_status",
                pages=pages,
                store=state.artifacts,
                max_result_tokens=settings.mcp_max_result_tokens,
                payload=result,
                header=f"Job {job_id} ({job.type}) completed.",
            )
            call.result_tokens = budgeted.text_tokens
            return _ok(budgeted.blocks, structured)


def _engine(state: McpState, settings: Settings) -> DeepResearchEngine:
    """The research engine, configured exactly as `POST /v1/research` configures it."""
    return DeepResearchEngine(
        searxng=state.searxng,
        crawler=state.crawler,
        llm=state.llm,
        reranker=state.reranker,
        concurrency=settings.research_concurrency,
        report_model=settings.report_model,
        report_token_budget=settings.research_report_token_budget,
        report_max_tokens=settings.llm_report_max_tokens,
        results_per_query=settings.research_results_per_query,
        crawl_top_k=settings.research_crawl_top_k,
        per_page_token_budget=settings.research_per_page_token_budget,
        process_context_token_budget=settings.research_process_context_token_budget,
        num_learnings=settings.research_num_learnings,
        num_followups=settings.research_num_followups,
        max_cost_usd=settings.research_max_cost_usd,
    )


def _job_result_pages(
    job_type: str, job_id: str, result: dict[str, Any]
) -> tuple[list[ArtifactPage], dict[str, Any]]:
    """Split a finished job's result into budgetable text and small metadata.

    A site crawl finishing with four hundred pages of markdown is exactly the
    payload §3 exists to stop: returning `result` verbatim here would undo the
    budgeting every other tool respects.
    """
    if job_type == "research":
        citations = "\n".join(f"[{c['index']}] {c['url']}" for c in result.get("citations", []))
        pages = [
            ArtifactPage(
                title="Report",
                text=f"{result.get('report', '')}\n\n### Sources\n{citations}".strip(),
            )
        ]
        structured = {
            "job_id": job_id,
            "type": job_type,
            "status": "completed",
            "query": result.get("query"),
            "sources": result.get("sources", []),
            "citations": result.get("citations", []),
            "learnings": result.get("learnings", []),
            "serp_queries": result.get("serp_queries", []),
            "stopped_early": result.get("stopped_early", False),
            "usage": result.get("usage", {}),
        }
        return pages, structured

    if job_type == "site_crawl":
        crawled = result.get("pages", [])
        pages = [
            ArtifactPage(
                title=page.get("title") or page.get("url", ""),
                text=page.get("markdown", "") or "",
                meta={"url": page.get("url"), "error": page.get("error")},
            )
            for page in crawled
        ]
        structured = {
            "job_id": job_id,
            "type": job_type,
            "status": "completed",
            "base_url": result.get("base_url"),
            "count": result.get("count", len(crawled)),
            "pages": [
                {
                    "url": page.get("url"),
                    "success": page.get("success"),
                    "status_code": page.get("status_code"),
                    "title": page.get("title"),
                    "word_count": page.get("word_count"),
                    "error": page.get("error"),
                }
                for page in crawled
            ],
        }
        return pages, structured

    # Any future job type still gets budgeted rather than dumped verbatim.
    return (
        [ArtifactPage(title=job_type, text=json.dumps(result, indent=2, ensure_ascii=False))],
        {"job_id": job_id, "type": job_type, "status": "completed"},
    )
