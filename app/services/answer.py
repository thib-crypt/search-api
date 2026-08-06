"""Single-pass, source-grounded question answering.

Pipeline: SearXNG search -> (optional) crawl the top results -> one LLM pass that
synthesizes an answer with inline [n] citations. Fast, cheaper alternative to the
full deep-research engine.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from pydantic import AnyHttpUrl

from app.core import metrics
from app.core.cache import TTLCache
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.llm.tokens import trim_to_tokens
from app.core.llm.usage import UsageTracker
from app.core.net.guard import BlockedUrlError
from app.core.search.searxng import SearxngClient
from app.models.answer import AnswerRequest, AnswerResponse, Source
from app.models.crawl import CrawlRequest
from app.models.search import SearchRequest
from app.models.usage import Usage

logger = logging.getLogger("unified_search_api.answer")

# Total budget for the source context fed to the model.
_CONTEXT_TOKEN_BUDGET = 6000

# Called with the number of pages about to be crawled, so a streaming caller can
# announce the crawl before it blocks on it.
CrawlHook = Callable[[int], Awaitable[None]]


def _norm_url(url: str) -> str:
    return url.rstrip("/")


_SYSTEM_PROMPT = (
    "You are a precise research assistant. Answer the user's question using ONLY the "
    "numbered sources provided. Cite the sources you use with inline markers like [1], [2]. "
    "If the sources do not contain the answer, say so plainly. Be concise and well-structured. "
    "Always respond in the SAME LANGUAGE as the user's question."
)


class AnswerService:
    def __init__(
        self,
        searxng: SearxngClient,
        crawler: CrawlerService,
        llm: LLMClient,
        cache: TTLCache[AnswerResponse] | None = None,
    ) -> None:
        self._searxng = searxng
        self._crawler = crawler
        self._llm = llm
        # Owned by the app (app.state), not by this instance: the service is
        # rebuilt per request, the cache must outlive it.
        self._cache = cache

    def _cache_key(self, req: AnswerRequest) -> str:
        """Key from (query, model, max_sources, language), per §5.7.

        `fetch_full` and `categories` are deliberately *not* in the key: the plan
        fixes this tuple, and the fields it leaves out change how the answer is
        produced far less than they change what it costs.
        """
        return "\x1f".join(
            (
                req.query,
                req.model or self._llm.default_model,
                str(req.max_sources),
                req.language or "",
            )
        )

    async def _fetch_full_text(
        self, sources: list[Source], usage: UsageTracker | None = None
    ) -> dict[str, str]:
        """Crawl the sources, best-effort. Snippets are the fallback, not an error.

        The network guard can refuse every result (a narrow domain allowlist, say);
        answering from snippets beats failing the request.
        """
        try:
            pages = await self._crawler.crawl(
                CrawlRequest(urls=[AnyHttpUrl(s.url) for s in sources], content_filter="pruning")
            )
        except BlockedUrlError as exc:
            logger.info("Answer crawl skipped, every source was blocked: %s", exc)
            return {}
        fetched = {_norm_url(p.url): p.markdown for p in pages if p.success and p.markdown}
        if usage is not None:
            usage.record_pages_crawled(len(fetched))
        return fetched

    async def _prepare_context(
        self,
        req: AnswerRequest,
        on_crawl: CrawlHook | None = None,
        usage: UsageTracker | None = None,
    ) -> tuple[list[Source], str]:
        """Search, optionally crawl, and build the numbered source context.

        Everything the two public methods share lives here; they differ only in
        how they call the model. ``on_crawl`` lets the streaming variant announce
        the crawl before it blocks on it.
        """
        search = await self._searxng.search(
            SearchRequest(
                q=req.query,
                language=req.language,
                categories=req.categories,
                max_results=req.max_sources,
            )
        )
        if usage is not None:
            usage.record_search()
        top = search.results[: req.max_sources]

        sources: list[Source] = [
            Source(index=i + 1, title=r.title, url=r.url, snippet=r.snippet)
            for i, r in enumerate(top)
        ]

        # Map url -> full markdown (best-effort). URLs are normalized because both
        # pydantic's AnyHttpUrl and the crawler may add/drop a trailing slash.
        full_text: dict[str, str] = {}
        if req.fetch_full and top:
            if on_crawl is not None:
                await on_crawl(len(top))
            full_text = await self._fetch_full_text(sources, usage)

        per_source_budget = max(_CONTEXT_TOKEN_BUDGET // max(len(sources), 1), 200)
        blocks: list[str] = []
        for s in sources:
            body = full_text.get(_norm_url(s.url), "")
            if body:
                s.used_full_text = True
            else:
                body = s.snippet
            body = await trim_to_tokens(body, per_source_budget)
            blocks.append(f"[{s.index}] {s.title}\nURL: {s.url}\n{body}")

        context = "\n\n---\n\n".join(blocks) if blocks else "(no sources found)"
        return sources, context

    def _messages(self, req: AnswerRequest, context: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Question: {req.query}\n\nSources:\n\n{context}"},
        ]

    async def answer(self, req: AnswerRequest, usage: UsageTracker | None = None) -> AnswerResponse:
        key = self._cache_key(req)
        if self._cache is not None:
            cached = await self._cache.get(key)
            if cached is not None:
                metrics.record_cache("answer", hit=True)
                # A private copy, so a caller mutating its response cannot poison
                # the entry every later caller receives.
                hit = cached.model_copy(deep=True)
                # Serving from cache costs nothing, so it reports nothing: the
                # stored usage belongs to the run that filled the entry, and
                # replaying it would double-bill the key that gets the hit.
                hit.usage = Usage()
                return hit
            metrics.record_cache("answer", hit=False)

        sources, context = await self._prepare_context(req, usage=usage)
        answer_text = await self._llm.complete(
            self._messages(req, context), model=req.model, usage=usage
        )
        response = AnswerResponse(
            query=req.query,
            answer=answer_text,
            model=req.model or self._llm.default_model,
            sources=sources,
            usage=Usage.from_tracker(usage) if usage is not None else Usage(),
        )
        if self._cache is not None:
            await self._cache.set(key, response)
        return response.model_copy(deep=True)

    async def stream_answer(
        self, req: AnswerRequest, usage: UsageTracker | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield SSE-friendly dicts: searching → crawling → token… → done.

        Not cached, and does not populate the cache: the value of streaming is the
        first token arriving early, and replaying a finished answer as a fake token
        stream would only re-serialize work the cache already holds. A client that
        wants the cheap path asks for `stream=false`.
        """
        import json

        yield {"event": "searching", "data": json.dumps({"query": req.query})}

        # The shared preparation runs as a task and reports through a queue: a
        # plain `await` would only surface the "crawling" event once the crawl it
        # announces had already finished, which is the opposite of its purpose.
        events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def on_crawl(count: int) -> None:
            await events.put({"event": "crawling", "data": json.dumps({"count": count})})

        prepare = asyncio.ensure_future(self._prepare_context(req, on_crawl=on_crawl, usage=usage))
        prepare.add_done_callback(lambda _: events.put_nowait(None))
        try:
            while (event := await events.get()) is not None:
                yield event
            sources, context = await prepare  # re-raises whatever the task hit
        except BaseException:  # client disconnected, or preparation failed
            prepare.cancel()
            raise

        yield {"event": "generating", "data": json.dumps({})}
        stream = self._llm.stream_complete(
            self._messages(req, context), model=req.model, usage=usage
        )
        async for token in stream:
            yield {"event": "token", "data": json.dumps({"token": token})}

        # §6.5: the final event carries the bill. It is emitted after the stream
        # closes, which is when the usage-bearing chunk has been seen.
        yield {
            "event": "done",
            "data": json.dumps(
                {
                    "model": req.model or self._llm.default_model,
                    "sources": [s.model_dump() for s in sources],
                    "usage": usage.snapshot() if usage is not None else Usage().model_dump(),
                }
            ),
        }
