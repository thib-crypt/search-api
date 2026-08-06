"""Deep-research engine — a Python port of dzhng/deep-research.

The engine recursively explores a topic:

    generate SERP queries (breadth)
        -> search (SearXNG), rerank on title/snippet, crawl only the top-k
        -> extract learnings + follow-up questions (LLM, structured)
        -> recurse with halved breadth and depth-1, seeded by the follow-ups
    -> aggregate learnings + sources
    -> write the final Markdown report (LLM, free text with inline citations)

Two run-wide guards keep the cost of a wide run bounded: a single semaphore
(concurrency is *not* per recursion level) and a single `visited` URL set (a page
crawled by one branch is never crawled again by another).

It depends only on the injected SearXNG / crawler / LLM / reranker clients, so it
is fully testable without network access.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import AnyHttpUrl

from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.llm.tokens import count_tokens, trim_to_tokens
from app.core.llm.usage import UsageTracker
from app.core.net.guard import BlockedUrlError
from app.core.research import prompts
from app.core.research.dedup import dedupe_contents, normalize_url
from app.core.research.rerank import NoopReranker, Reranker
from app.core.research.schemas import (
    FeedbackQuestions,
    Learning,
    SerpProcessing,
    SerpQuery,
    SerpQueryList,
)
from app.core.search.searxng import SearxngClient
from app.models.crawl import CrawlRequest
from app.models.research import Citation
from app.models.search import SearchRequest, TimeRange

logger = logging.getLogger("unified_search_api.research")

# Defaults for the tuning knobs. The real values come from `Settings`
# (`RESEARCH_*` / `LLM_REPORT_MAX_TOKENS`); these keep the engine constructible
# — and unit-testable — without one.
DEFAULT_RESULTS_PER_QUERY = 10
DEFAULT_CRAWL_TOP_K = 3
DEFAULT_PER_PAGE_TOKEN_BUDGET = 2000
DEFAULT_PROCESS_CONTEXT_TOKEN_BUDGET = 8000
DEFAULT_NUM_LEARNINGS = 3
DEFAULT_NUM_FOLLOWUPS = 3
DEFAULT_REPORT_TOKEN_BUDGET = 12000
DEFAULT_REPORT_MAX_TOKENS = 8192


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
# Called once per token of the final report. Same shape as ProgressHook: optional,
# awaited, and completely absent from the non-streaming path.
TokenHook = Callable[[str], Awaitable[None]]


@dataclass
class ResearchResult:
    report: str
    learnings: list[Learning]
    sources: list[str]
    serp_queries: list[str]
    citations: list[Citation] = field(default_factory=list)
    # §6.5: tokens, dollars and page counts for this run.
    usage: UsageTracker = field(default_factory=UsageTracker)
    # §6.4a: true when RESEARCH_MAX_COST_USD cut the exploration short. The run
    # still produced a report — from what it had gathered when the cap hit.
    stopped_early: bool = False


@dataclass
class _RunState:
    model: str | None
    total_depth: int
    on_progress: ProgressHook | None
    # One semaphore for the whole run. Created per level, concurrency would be
    # `concurrency ** depth`: each recursive call would hand its branch a fresh budget.
    semaphore: asyncio.Semaphore
    # §6.5: one tracker for the whole run, shared by every branch. Mutated from
    # several coroutines, which is safe here: the event loop is single-threaded
    # and every update is a plain `+=` between await points.
    usage: UsageTracker = field(default_factory=UsageTracker)
    # §6.4a: 0 disables. Checked at branch boundaries, never mid-LLM-call.
    max_cost_usd: float = 0.0
    stopped_early: bool = False
    # SERP filters (§5.6): applied to every search the run makes, at every depth.
    time_range: TimeRange | None = None
    include_domains: list[str] | None = None
    exclude_domains: list[str] | None = None
    # Normalized URLs already claimed by some branch of this run. Neighbouring
    # SERPs overlap heavily; without this the same page is crawled several times.
    visited: set[str] = field(default_factory=set)
    serp_queries: list[str] = field(default_factory=list)
    completed: int = 0
    total: int = 0

    def over_budget(self) -> bool:
        """True once the run has spent past its cap (0 = no cap).

        Deliberately consulted only *between* units of work: aborting a call
        already in flight would pay for it and throw the answer away.
        """
        if self.max_cost_usd <= 0:
            return False
        if self.usage.total_cost_usd < self.max_cost_usd:
            return False
        self.stopped_early = True
        return True


# Words that signal a request too vague to research as-is.
_VAGUE_TERMS = frozenset(
    {
        "stuff",
        "things",
        "something",
        "anything",
        "etc",
        "whatever",
        "truc",
        "trucs",
        "machin",
        "des",
    }
)
_MIN_SPECIFIC_WORDS = 8
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def query_looks_specific(query: str) -> bool:
    """Cheap pre-filter for the clarification step (§4.4).

    In ``interactive="auto"`` mode every request used to pay a full LLM
    round-trip before any useful work started, only for the model to answer "no
    questions" most of the time. A long query with no vague filler is taken as
    specific enough to research directly; anything shorter or fuzzier still goes
    through the model, so the heuristic can only skip a call it was going to
    waste, never decide *for* clarification.
    """
    words = _WORD_RE.findall(query.lower())
    if len(words) < _MIN_SPECIFIC_WORDS:
        return False
    return not _VAGUE_TERMS.intersection(words)


async def _fit_learnings(learnings: list[Learning], budget: int) -> list[Learning]:
    """Keep as many learnings as fit in ``budget`` tokens, earliest first.

    A `breadth=8, depth=3` run accumulates enough learnings to blow past the
    model's context window. Overflowing there wastes the entire run — ten minutes
    of crawling and LLM calls — at the very last step, so the prompt is bounded
    even if that means dropping the tail.
    """
    if budget <= 0:
        return learnings
    kept: list[Learning] = []
    used = 0
    for learning in learnings:
        cost = await count_tokens(learning.text) + 2  # + list formatting
        if used + cost > budget:
            if not kept:  # a single oversized learning still has to fit
                kept.append(
                    Learning(
                        text=await trim_to_tokens(learning.text, budget),
                        source_urls=learning.source_urls,
                    )
                )
            break
        kept.append(learning)
        used += cost
    return kept


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _dedupe_learnings(learnings: list[Learning]) -> list[Learning]:
    """Merge learnings sharing the same text, unioning their sources.

    Branches re-derive the same fact from different pages; keeping one copy with
    both URLs is strictly better than keeping two copies with one URL each.
    """
    by_text: dict[str, Learning] = {}
    for learning in learnings:
        existing = by_text.get(learning.text)
        if existing is None:
            by_text[learning.text] = Learning(
                text=learning.text, source_urls=list(learning.source_urls)
            )
            continue
        for url in learning.source_urls:
            if url not in existing.source_urls:
                existing.source_urls.append(url)
    return list(by_text.values())


class DeepResearchEngine:
    def __init__(
        self,
        searxng: SearxngClient,
        crawler: CrawlerService,
        llm: LLMClient,
        *,
        reranker: Reranker | None = None,
        concurrency: int = 2,
        report_model: str | None = None,
        report_token_budget: int = DEFAULT_REPORT_TOKEN_BUDGET,
        report_max_tokens: int = DEFAULT_REPORT_MAX_TOKENS,
        results_per_query: int = DEFAULT_RESULTS_PER_QUERY,
        crawl_top_k: int = DEFAULT_CRAWL_TOP_K,
        per_page_token_budget: int = DEFAULT_PER_PAGE_TOKEN_BUDGET,
        process_context_token_budget: int = DEFAULT_PROCESS_CONTEXT_TOKEN_BUDGET,
        num_learnings: int = DEFAULT_NUM_LEARNINGS,
        num_followups: int = DEFAULT_NUM_FOLLOWUPS,
        max_cost_usd: float = 0.0,
    ) -> None:
        self._searxng = searxng
        self._crawler = crawler
        self._llm = llm
        # Default to a no-op so the engine stays constructible in tests; the app
        # injects the configured implementation at startup.
        self._reranker: Reranker = reranker or NoopReranker()
        self._concurrency = max(concurrency, 1)
        self._report_model = report_model
        self._report_token_budget = report_token_budget
        self._report_max_tokens = report_max_tokens
        self._results_per_query = max(results_per_query, 1)
        self._crawl_top_k = max(crawl_top_k, 1)
        self._per_page_token_budget = per_page_token_budget
        self._process_context_token_budget = process_context_token_budget
        self._num_learnings = num_learnings
        self._num_followups = num_followups
        self._max_cost_usd = max(max_cost_usd, 0.0)

    # -- Public API ---------------------------------------------------------

    async def generate_feedback(
        self,
        query: str,
        *,
        max_questions: int = 3,
        model: str | None = None,
        force: bool = False,
        usage: UsageTracker | None = None,
    ) -> list[str]:
        result = await self._llm.structured(
            messages=self._messages(prompts.feedback_prompt(query, max_questions, force)),
            response_model=FeedbackQuestions,
            model=model,
            usage=usage,
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
        on_token: TokenHook | None = None,
        time_range: TimeRange | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        usage: UsageTracker | None = None,
    ) -> ResearchResult:
        state = _RunState(
            model=model,
            total_depth=depth,
            on_progress=on_progress,
            semaphore=asyncio.Semaphore(self._concurrency),
            # The caller may hand in a tracker it already holds (the route needs
            # it to bill the key even if the run raises); otherwise one per run.
            usage=usage if usage is not None else UsageTracker(),
            max_cost_usd=self._max_cost_usd,
            time_range=time_range,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
        )
        learnings, sources = await self._deep_research(
            query, breadth=breadth, depth=depth, learnings=[], sources=[], state=state
        )
        learnings = _dedupe_learnings(learnings)
        sources = _dedupe(sources)
        citations = [Citation(index=i + 1, url=url) for i, url in enumerate(sources)]
        # The report is written no matter what: an early stop must look like a
        # shorter run, not a failed one.
        report = await self._write_report(
            query,
            learnings,
            sources,
            model,
            on_token=on_token,
            state=state,
            truncated=state.stopped_early,
        )
        return ResearchResult(
            report=report,
            learnings=learnings,
            sources=sources,
            serp_queries=_dedupe(state.serp_queries),
            citations=citations,
            usage=state.usage,
            stopped_early=state.stopped_early,
        )

    # -- Recursive core -----------------------------------------------------

    async def _deep_research(
        self,
        query: str,
        *,
        breadth: int,
        depth: int,
        learnings: list[Learning],
        sources: list[str],
        state: _RunState,
    ) -> tuple[list[Learning], list[str]]:
        # §6.4a: the cap is checked before every unit of work that costs money —
        # here, before the query-generation call that opens a whole new level.
        if state.over_budget():
            return learnings, sources
        queries = await self._generate_serp_queries(
            query, breadth, [item.text for item in learnings], state.model, state.usage
        )
        state.total += len(queries)

        async def handle(serp_query: SerpQuery) -> tuple[list[Learning], list[str]]:
            # The permit covers the work of this level only. Recursing while still
            # holding it would deadlock: with a single run-wide semaphore, parents
            # would occupy every permit waiting on children that can never get one.
            async with state.semaphore:
                # Branches run concurrently, so the cap is re-checked once this
                # one actually holds a permit: an earlier branch may have spent
                # the remaining budget while this one was queued.
                if state.over_budget():
                    return learnings, sources
                state.serp_queries.append(serp_query.query)
                await self._emit(state, "searching", depth, serp_query.query)

                visited, contents = await self._gather_sources(
                    serp_query.query, serp_query.research_goal, state
                )
                new_learnings, follow_ups = await self._process_contents(
                    serp_query.query, contents, visited, state.model, state.usage
                )

                branch_learnings = learnings + new_learnings
                branch_sources = sources + visited
                state.completed += 1
                await self._emit(state, "processed", depth, serp_query.query)

            if depth - 1 > 0 and follow_ups and not state.over_budget():
                next_query = (
                    f"Previous research goal: {serp_query.research_goal}\n"
                    "Follow-up research directions:\n" + "\n".join(follow_ups)
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
        return _dedupe_learnings(all_learnings), _dedupe(all_sources)

    # -- Steps --------------------------------------------------------------

    async def _generate_serp_queries(
        self,
        query: str,
        num: int,
        learnings: list[str],
        model: str | None,
        usage: UsageTracker | None = None,
    ) -> list[SerpQuery]:
        result = await self._llm.structured(
            messages=self._messages(prompts.serp_queries_prompt(query, num, learnings)),
            response_model=SerpQueryList,
            model=model,
            usage=usage,
        )
        return result.queries[:num]

    async def _gather_sources(
        self, query: str, goal: str, state: _RunState
    ) -> tuple[list[str], list[str]]:
        """Search wide, crawl narrow. Returns (crawled URLs, page contents)."""
        search = await self._searxng.search(
            SearchRequest(
                q=query,
                max_results=self._results_per_query,
                # §5.6: the request-level filters really do reach SearXNG (time_range)
                # and really do prune the candidate set (domains) before any crawl.
                time_range=state.time_range,
                include_domains=state.include_domains,
                exclude_domains=state.exclude_domains,
            )
        )
        state.usage.record_search()
        # Drop what another branch already took *before* ranking, so this branch
        # still gets a full crawl budget of pages nobody has fetched yet.
        candidates = [r for r in search.results if normalize_url(r.url) not in state.visited]
        if not candidates:
            return [], []

        ranked = await self._reranker.rank(
            goal or query,
            candidates,
            self._crawl_top_k,
            time_range=state.time_range,
            usage=state.usage,
        )
        # §6.5: everything the SERP offered that the reranker did not keep is a
        # page (and ~2000 tokens of context) the run did not pay for.
        state.usage.record_pages_skipped(len(candidates) - len(ranked))

        urls: list[str] = []
        for item in ranked:
            key = normalize_url(item.url)
            # Re-checked here because the rerank above is an await point: a
            # concurrent branch may have claimed the URL in the meantime. The
            # claim is taken before the crawl, so a page that fails to fetch is
            # not retried by every other branch that surfaces it.
            if not key or key in state.visited:
                continue
            state.visited.add(key)
            urls.append(item.url)
        if not urls:
            return [], []

        try:
            pages = await self._crawler.crawl(
                # `urls` are plain strings here; the model declares `AnyHttpUrl`, so
                # validate them explicitly rather than relying on coercion mypy
                # cannot see.
                CrawlRequest(urls=[AnyHttpUrl(u) for u in urls], content_filter="pruning")
            )
        except BlockedUrlError as exc:
            # Every result was refused by the network guard. One barren branch
            # must not take down a ten-minute run.
            logger.info("No crawlable source for %r: %s", query, exc)
            return [], []

        visited: list[str] = []
        contents: list[str] = []
        for page in pages:
            if page.success and page.markdown:
                visited.append(page.url)
                state.usage.record_pages_crawled(1)
                contents.append(await trim_to_tokens(page.markdown, self._per_page_token_budget))

        # Several outlets republishing one wire story would otherwise be sent to
        # the model in full, once each.
        kept, dropped = dedupe_contents(contents)
        if dropped:
            logger.debug(
                "Dropped %d near-duplicate page(s) for %r: %s",
                len(dropped),
                query,
                [visited[i] for i in dropped],
            )
        return [visited[i] for i in kept], [contents[i] for i in kept]

    async def _process_contents(
        self,
        query: str,
        contents: list[str],
        urls: list[str],
        model: str | None,
        usage: UsageTracker | None = None,
    ) -> tuple[list[Learning], list[str]]:
        if not contents:
            return [], []
        budget = max(self._process_context_token_budget // len(contents), 200)
        trimmed = [await trim_to_tokens(c, budget) for c in contents]
        processed = await self._llm.structured(
            messages=self._messages(
                prompts.process_result_prompt(
                    query, trimmed, self._num_learnings, self._num_followups, urls
                )
            ),
            response_model=SerpProcessing,
            model=model,
            usage=usage,
        )
        learnings = [
            Learning(text=draft.text, source_urls=self._resolve_sources(draft.source_indices, urls))
            for draft in processed.learnings
        ]
        return learnings, list(processed.follow_up_questions)

    @staticmethod
    def _resolve_sources(indices: list[int], urls: list[str]) -> list[str]:
        """Map the model's 1-based indices to URLs, ignoring anything out of range."""
        out: list[str] = []
        for index in indices:
            if 1 <= index <= len(urls) and urls[index - 1] not in out:
                out.append(urls[index - 1])
        return out

    async def _write_report(
        self,
        query: str,
        learnings: list[Learning],
        sources: list[str],
        model: str | None,
        *,
        on_token: TokenHook | None = None,
        state: _RunState | None = None,
        truncated: bool = False,
    ) -> str:
        """Free-text completion, not a structured call.

        A multi-page report squeezed through a JSON string field pays for its own
        escaping, cannot be streamed, and dies on a parse error at the very last
        step of the run.

        With ``on_token`` set the same prompt is streamed instead, so a caller can
        show the report as it is written; the assembled text is identical either way.
        """
        kept = await _fit_learnings(learnings, self._report_token_budget)
        index_of = {url: i + 1 for i, url in enumerate(sources)}
        cited: list[str] = []
        for item in kept:
            markers = "".join(f"[{index_of[u]}]" for u in item.source_urls if u in index_of)
            cited.append(f"{item.text} {markers}".strip())
        messages = self._messages(prompts.final_report_prompt(query, cited, sources))
        usage = state.usage if state is not None else None
        if on_token is None:
            report = await self._llm.complete(
                messages,
                model=self._report_model or model,
                max_tokens=self._report_max_tokens,
                usage=usage,
            )
        else:
            chunks: list[str] = []
            stream = self._llm.stream_complete(
                messages,
                model=self._report_model or model,
                max_tokens=self._report_max_tokens,
                usage=usage,
            )
            async for token in stream:
                chunks.append(token)
                await on_token(token)
            report = "".join(chunks)
        report = report.rstrip()
        if truncated:
            # Stated in the report itself, not only in a response field: whoever
            # reads the markdown later must know it covers a partial exploration.
            report += (
                "\n\n> **Note** — this run reached its cost ceiling "
                "(`RESEARCH_MAX_COST_USD`) and stopped exploring early. "
                "The findings above come from the sources gathered before the cap "
                "was reached; widening the budget would deepen the coverage.\n"
            )
        if sources:
            source_list = "\n".join(f"[{i + 1}] {url}" for i, url in enumerate(sources))
            report += f"\n\n## Sources\n\n{source_list}\n"
        return report

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _messages(user_content: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": prompts.system_prompt()},
            {"role": "user", "content": user_content},
        ]

    async def _emit(self, state: _RunState, stage: str, depth: int, message: str) -> None:
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
