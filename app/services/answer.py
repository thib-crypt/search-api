"""Single-pass, source-grounded question answering.

Pipeline: SearXNG search -> (optional) crawl the top results -> one LLM pass that
synthesizes an answer with inline [n] citations. Fast, cheaper alternative to the
full deep-research engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.llm.tokens import trim_to_tokens
from app.core.search.searxng import SearxngClient
from app.models.answer import AnswerRequest, AnswerResponse, Source
from app.models.crawl import CrawlRequest
from app.models.search import SearchRequest

# Total budget for the source context fed to the model.
_CONTEXT_TOKEN_BUDGET = 6000

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
        self, searxng: SearxngClient, crawler: CrawlerService, llm: LLMClient
    ) -> None:
        self._searxng = searxng
        self._crawler = crawler
        self._llm = llm

    async def answer(self, req: AnswerRequest) -> AnswerResponse:
        search = await self._searxng.search(
            SearchRequest(
                q=req.query,
                language=req.language,
                categories=req.categories,
                max_results=req.max_sources,
            )
        )
        top = search.results[: req.max_sources]

        sources: list[Source] = [
            Source(index=i + 1, title=r.title, url=r.url, snippet=r.snippet)
            for i, r in enumerate(top)
        ]

        # Map url -> full markdown (best-effort). URLs are normalized because both
        # pydantic's AnyHttpUrl and the crawler may add/drop a trailing slash.
        full_text: dict[str, str] = {}
        if req.fetch_full and top:
            pages = await self._crawler.crawl(
                CrawlRequest(urls=[s.url for s in sources], content_filter="pruning")
            )
            full_text = {
                _norm_url(p.url): p.markdown for p in pages if p.success and p.markdown
            }

        per_source_budget = max(_CONTEXT_TOKEN_BUDGET // max(len(sources), 1), 200)
        blocks: list[str] = []
        for s in sources:
            body = full_text.get(_norm_url(s.url), "")
            if body:
                s.used_full_text = True
            else:
                body = s.snippet
            body = trim_to_tokens(body, per_source_budget)
            blocks.append(f"[{s.index}] {s.title}\nURL: {s.url}\n{body}")

        context = "\n\n---\n\n".join(blocks) if blocks else "(no sources found)"
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Question: {req.query}\n\nSources:\n\n{context}",
            },
        ]

        answer_text = await self._llm.complete(messages, model=req.model)
        return AnswerResponse(
            query=req.query,
            answer=answer_text,
            model=req.model or self._llm.default_model,
            sources=sources,
        )

    async def stream_answer(self, req: AnswerRequest) -> AsyncIterator[dict]:
        """Yield SSE-friendly dicts: searching → crawling → token… → done."""
        import json

        yield {"event": "searching", "data": json.dumps({"query": req.query})}

        search = await self._searxng.search(
            SearchRequest(
                q=req.query,
                language=req.language,
                categories=req.categories,
                max_results=req.max_sources,
            )
        )
        top = search.results[: req.max_sources]
        sources: list[Source] = [
            Source(index=i + 1, title=r.title, url=r.url, snippet=r.snippet)
            for i, r in enumerate(top)
        ]

        full_text: dict[str, str] = {}
        if req.fetch_full and top:
            yield {"event": "crawling", "data": json.dumps({"count": len(top)})}
            pages = await self._crawler.crawl(
                CrawlRequest(urls=[s.url for s in sources], content_filter="pruning")
            )
            full_text = {
                _norm_url(p.url): p.markdown for p in pages if p.success and p.markdown
            }

        per_source_budget = max(_CONTEXT_TOKEN_BUDGET // max(len(sources), 1), 200)
        blocks: list[str] = []
        for s in sources:
            body = full_text.get(_norm_url(s.url), "")
            if body:
                s.used_full_text = True
            else:
                body = s.snippet
            body = trim_to_tokens(body, per_source_budget)
            blocks.append(f"[{s.index}] {s.title}\nURL: {s.url}\n{body}")

        context = "\n\n---\n\n".join(blocks) if blocks else "(no sources found)"
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Question: {req.query}\n\nSources:\n\n{context}"},
        ]

        yield {"event": "generating", "data": json.dumps({})}
        async for token in self._llm.stream_complete(messages, model=req.model):
            yield {"event": "token", "data": json.dumps({"token": token})}

        yield {
            "event": "done",
            "data": json.dumps({
                "model": req.model or self._llm.default_model,
                "sources": [s.model_dump() for s in sources],
            }),
        }
