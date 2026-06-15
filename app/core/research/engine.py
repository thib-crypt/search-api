"""Deep-research engine — a Python port of dzhng/deep-research.

The engine recursively explores a topic:

    generate SERP queries (breadth)
        -> search (SearXNG) + crawl (crawl4ai)
        -> extract learnings + follow-up questions (LLM, structured)
        -> recurse with halved breadth and depth-1, seeded by the follow-ups
    -> aggregate learnings + sources
    -> write the final Markdown report (LLM)

It depends only on the injected SearXNG / crawler / LLM clients, so it is fully
testable without network access.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.llm.tokens import trim_to_tokens
from app.core.research import prompts
from app.core.research.schemas import (
    FeedbackQuestions,
    ReportDraft,
    SerpProcessing,
    SerpQueryList,
)
from app.core.search.searxng import SearxngClient
from app.models.crawl import CrawlRequest
from app.models.search import SearchRequest

# Tuning constants.
RESULTS_PER_QUERY = 5
PER_PAGE_TOKEN_BUDGET = 2000
PROCESS_CONTEXT_TOKEN_BUDGET = 8000
NUM_LEARNINGS = 3
NUM_FOLLOWUPS = 3


@dataclass
class ResearchProgress:
    """Snapshot emitted as research advances (consumed by SSE in a later phase)."""

    stage: str
    current_depth: int
    total_depth: int
    completed_queries: int
    total_queries: int
    message: str = ""


ProgressHook = Callable[[ResearchProgress], Awaitable[None]]


@dataclass
class ResearchResult:
    report: str
    learnings: list[str]
    sources: list[str]
    serp_queries: list[str]


@dataclass
class _RunState:
    model: str | None
    total_depth: int
    on_progress: ProgressHook | None
    serp_queries: list[str] = field(default_factory=list)
    completed: int = 0
    total: int = 0


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class DeepResearchEngine:
    def __init__(
        self,
        searxng: SearxngClient,
        crawler: CrawlerService,
        llm: LLMClient,
        *,
        concurrency: int = 2,
        report_model: str | None = None,
    ) -> None:
        self._searxng = searxng
        self._crawler = crawler
        self._llm = llm
        self._concurrency = max(concurrency, 1)
        self._report_model = report_model

    # -- Public API ---------------------------------------------------------

    async def generate_feedback(
        self,
        query: str,
        *,
        max_questions: int = 3,
        model: str | None = None,
        force: bool = False,
    ) -> list[str]:
        result = await self._llm.structured(
            messages=self._messages(prompts.feedback_prompt(query, max_questions, force)),
            response_model=FeedbackQuestions,
            model=model,
        )
        return result.questions[:max_questions]

    async def run(
        self,
        query: str,
        *,
        breadth: int,
        depth: int,
        model: str | None = None,
        on_progress: ProgressHook | None = None,
    ) -> ResearchResult:
        state = _RunState(model=model, total_depth=depth, on_progress=on_progress)
        learnings, sources = await self._deep_research(
            query, breadth=breadth, depth=depth, learnings=[], sources=[], state=state
        )
        learnings = _dedupe(learnings)
        sources = _dedupe(sources)
        report = await self._write_report(query, learnings, sources, model)
        return ResearchResult(
            report=report,
            learnings=learnings,
            sources=sources,
            serp_queries=_dedupe(state.serp_queries),
        )

    # -- Recursive core -----------------------------------------------------

    async def _deep_research(
        self,
        query: str,
        *,
        breadth: int,
        depth: int,
        learnings: list[str],
        sources: list[str],
        state: _RunState,
    ) -> tuple[list[str], list[str]]:
        queries = await self._generate_serp_queries(query, breadth, learnings, state.model)
        state.total += len(queries)
        semaphore = asyncio.Semaphore(self._concurrency)

        async def handle(serp_query) -> tuple[list[str], list[str]]:
            async with semaphore:
                state.serp_queries.append(serp_query.query)
                await self._emit(state, "searching", depth, serp_query.query)

                visited, contents = await self._gather_sources(serp_query.query)
                processed = await self._process_contents(serp_query.query, contents, state.model)

                branch_learnings = learnings + processed.learnings
                branch_sources = sources + visited
                state.completed += 1
                await self._emit(state, "processed", depth, serp_query.query)

                if depth - 1 > 0 and processed.follow_up_questions:
                    next_query = (
                        f"Previous research goal: {serp_query.research_goal}\n"
                        "Follow-up research directions:\n"
                        + "\n".join(processed.follow_up_questions)
                    )
                    return await self._deep_research(
                        next_query,
                        breadth=math.ceil(breadth / 2),
                        depth=depth - 1,
                        learnings=branch_learnings,
                        sources=branch_sources,
                        state=state,
                    )
                return branch_learnings, branch_sources

        results = await asyncio.gather(*(handle(q) for q in queries))
        all_learnings = [item for branch, _ in results for item in branch]
        all_sources = [item for _, branch in results for item in branch]
        return _dedupe(all_learnings), _dedupe(all_sources)

    # -- Steps --------------------------------------------------------------

    async def _generate_serp_queries(
        self, query: str, num: int, learnings: list[str], model: str | None
    ) -> list:
        result = await self._llm.structured(
            messages=self._messages(prompts.serp_queries_prompt(query, num, learnings)),
            response_model=SerpQueryList,
            model=model,
        )
        return result.queries[:num]

    async def _gather_sources(self, query: str) -> tuple[list[str], list[str]]:
        search = await self._searxng.search(
            SearchRequest(q=query, max_results=RESULTS_PER_QUERY)
        )
        urls = [r.url for r in search.results]
        if not urls:
            return [], []
        pages = await self._crawler.crawl(
            CrawlRequest(urls=urls, content_filter="pruning")
        )
        visited: list[str] = []
        contents: list[str] = []
        for page in pages:
            if page.success and page.markdown:
                visited.append(page.url)
                contents.append(trim_to_tokens(page.markdown, PER_PAGE_TOKEN_BUDGET))
        return visited, contents

    async def _process_contents(
        self, query: str, contents: list[str], model: str | None
    ) -> SerpProcessing:
        if not contents:
            return SerpProcessing()
        budget = max(PROCESS_CONTEXT_TOKEN_BUDGET // len(contents), 200)
        trimmed = [trim_to_tokens(c, budget) for c in contents]
        return await self._llm.structured(
            messages=self._messages(
                prompts.process_result_prompt(query, trimmed, NUM_LEARNINGS, NUM_FOLLOWUPS)
            ),
            response_model=SerpProcessing,
            model=model,
        )

    async def _write_report(
        self, query: str, learnings: list[str], sources: list[str], model: str | None
    ) -> str:
        draft = await self._llm.structured(
            messages=self._messages(prompts.final_report_prompt(query, learnings)),
            response_model=ReportDraft,
            model=self._report_model or model,
        )
        report = draft.report_markdown.rstrip()
        if sources:
            source_list = "\n".join(f"- {url}" for url in sources)
            report += f"\n\n## Sources\n\n{source_list}\n"
        return report

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _messages(user_content: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": prompts.system_prompt()},
            {"role": "user", "content": user_content},
        ]

    async def _emit(
        self, state: _RunState, stage: str, depth: int, message: str
    ) -> None:
        if state.on_progress is None:
            return
        await state.on_progress(
            ResearchProgress(
                stage=stage,
                current_depth=state.total_depth - depth + 1,
                total_depth=state.total_depth,
                completed_queries=state.completed,
                total_queries=state.total,
                message=message,
            )
        )
