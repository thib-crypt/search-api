"""Crawling service built on crawl4ai.

Wraps a single long-lived ``AsyncWebCrawler`` (one browser pool for the process)
and exposes a small, typed surface used by the API. Single and multi-URL crawls
both flow through :meth:`crawl`; multi-URL uses crawl4ai's memory-adaptive
dispatcher for bounded concurrency and per-host rate limiting.
"""

from __future__ import annotations

from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    MemoryAdaptiveDispatcher,
    RateLimiter,
)
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.filters import FilterChain, URLPatternFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from app.jobs.manager import Emit
from app.models.crawl import CrawledPage, CrawlRequest, SiteCrawlRequest


class CrawlerService:
    def __init__(self, max_concurrency: int = 5, timeout: float = 30.0) -> None:
        self._max_concurrency = max_concurrency
        self._timeout_ms = int(timeout * 1000)
        self._crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
        self._started = False

    async def start(self) -> None:
        if not self._started:
            await self._crawler.start()
            self._started = True

    async def close(self) -> None:
        if self._started:
            await self._crawler.close()
            self._started = False

    def _run_config(self, req: CrawlRequest) -> CrawlerRunConfig:
        content_filter = (
            PruningContentFilter(threshold=req.prune_threshold, threshold_type="dynamic")
            if req.content_filter == "pruning"
            else None
        )
        md_generator = DefaultMarkdownGenerator(
            content_filter=content_filter,
            options={"ignore_links": req.ignore_links},
        )
        return CrawlerRunConfig(
            markdown_generator=md_generator,
            cache_mode=CacheMode.ENABLED if req.cache else CacheMode.BYPASS,
            word_count_threshold=req.word_count_threshold,
            exclude_external_links=req.exclude_external_links,
            page_timeout=self._timeout_ms,
            stream=False,
        )

    async def crawl(self, req: CrawlRequest) -> list[CrawledPage]:
        await self.start()
        config = self._run_config(req)
        urls = [str(u) for u in req.urls]

        if len(urls) == 1:
            result = await self._crawler.arun(url=urls[0], config=config)
            return [
                self._to_page(
                    urls[0],
                    result,
                    content_filter=req.content_filter,
                    include_raw=req.include_raw_markdown,
                )
            ]

        dispatcher = MemoryAdaptiveDispatcher(
            max_session_permit=self._max_concurrency,
            rate_limiter=RateLimiter(base_delay=(1.0, 3.0), max_delay=60.0, max_retries=3),
        )
        results = await self._crawler.arun_many(urls=urls, config=config, dispatcher=dispatcher)
        return [
            self._to_page(
                getattr(r, "url", url),
                r,
                content_filter=req.content_filter,
                include_raw=req.include_raw_markdown,
            )
            for url, r in zip(urls, results, strict=False)
        ]

    async def crawl_site(
        self, req: SiteCrawlRequest, emit: Emit | None = None
    ) -> list[CrawledPage]:
        """Deep-crawl a whole site (BFS), streaming pages as they are discovered."""
        await self.start()

        filters = []
        if req.url_patterns:
            filters.append(URLPatternFilter(patterns=req.url_patterns))
        strategy = BFSDeepCrawlStrategy(
            max_depth=req.max_depth,
            max_pages=req.max_pages,
            include_external=req.include_external,
            filter_chain=FilterChain(filters),
        )
        content_filter = (
            PruningContentFilter(threshold=0.48, threshold_type="dynamic")
            if req.content_filter == "pruning"
            else None
        )
        config = CrawlerRunConfig(
            deep_crawl_strategy=strategy,
            markdown_generator=DefaultMarkdownGenerator(
                content_filter=content_filter, options={"ignore_links": True}
            ),
            cache_mode=CacheMode.BYPASS,
            page_timeout=self._timeout_ms,
            stream=True,
        )

        pages: list[CrawledPage] = []
        container = await self._crawler.arun(url=str(req.url), config=config)
        if hasattr(container, "__aiter__"):
            async for result in container:
                pages.append(await self._collect(result, req, emit, len(pages) + 1))
        else:  # non-streaming fallback
            for result in container:
                pages.append(await self._collect(result, req, emit, len(pages) + 1))
        return pages

    async def _collect(
        self, result: object, req: SiteCrawlRequest, emit: Emit | None, count: int
    ) -> CrawledPage:
        page = self._to_page(
            getattr(result, "url", str(req.url)),
            result,
            content_filter=req.content_filter,
        )
        if emit is not None:
            await emit({"stage": "crawled", "pages": count, "url": page.url})
        return page

    @staticmethod
    def _to_page(
        url: str,
        result: object,
        *,
        content_filter: str = "pruning",
        include_raw: bool = False,
    ) -> CrawledPage:
        if not getattr(result, "success", False):
            return CrawledPage(
                url=getattr(result, "url", url),
                success=False,
                status_code=getattr(result, "status_code", None),
                error=getattr(result, "error_message", None) or "crawl failed",
            )

        raw_md, fit_md = _extract_markdown(getattr(result, "markdown", None))
        chosen = fit_md if (content_filter == "pruning" and fit_md) else raw_md

        metadata = getattr(result, "metadata", None) or {}
        links = getattr(result, "links", None) or {}

        return CrawledPage(
            url=getattr(result, "url", url),
            success=True,
            status_code=getattr(result, "status_code", None),
            title=metadata.get("title") if isinstance(metadata, dict) else None,
            markdown=chosen,
            raw_markdown=raw_md if include_raw else None,
            word_count=len(chosen.split()),
            internal_links=len(links.get("internal", []) or []) if isinstance(links, dict) else 0,
            external_links=len(links.get("external", []) or []) if isinstance(links, dict) else 0,
        )


def _extract_markdown(md: object) -> tuple[str, str]:
    """Return (raw_markdown, fit_markdown). ``md`` may be a string or an object."""
    if md is None:
        return "", ""
    if isinstance(md, str):
        return md, ""
    raw = getattr(md, "raw_markdown", "") or ""
    fit = getattr(md, "fit_markdown", "") or ""
    return raw, fit
