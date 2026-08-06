"""Crawling service built on crawl4ai.

Wraps a single long-lived ``AsyncWebCrawler`` (one browser pool for the process)
and exposes a small, typed surface used by the API.

Three properties matter here and are enforced by this module, not by callers:

* **Every URL passes the SSRF guard** (`app.core.net.guard`) before the browser
  sees it, and again on the URL the crawl landed on, so a redirect cannot walk
  into the private network.
* **Concurrency is bounded process-wide.** A single ``asyncio.Semaphore`` owned
  by the service caps concurrent browser sessions across all in-flight requests;
  a per-request dispatcher could not (N requests × 5 sessions each).
* **Results are paired with the URL that was asked for.** Each URL is crawled
  through its own ``arun`` call, so a missing or reordered result cannot be
  silently attributed to the wrong URL — any URL without a result comes back as
  an explicit failure.
"""

from __future__ import annotations

import asyncio

from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
)
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.filters import FilterChain, URLPatternFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from app.core.net.guard import BlockedUrlError, UrlGuard
from app.jobs.manager import Emit
from app.models.crawl import CrawledPage, CrawlRequest, SiteCrawlRequest

# Above this many tracked hosts, expired politeness entries are swept.
_HOST_GATE_HIGH_WATER = 2048


class CrawlerService:
    def __init__(
        self,
        max_concurrency: int = 5,
        timeout: float = 30.0,
        *,
        guard: UrlGuard | None = None,
        per_host_delay: float = 1.0,
    ) -> None:
        self._max_concurrency = max(max_concurrency, 1)
        self._timeout_ms = int(timeout * 1000)
        self._crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
        self._started = False
        self._start_lock = asyncio.Lock()
        # Process-wide cap on concurrent browser sessions. Held for the duration of
        # one page fetch (or one whole site crawl).
        self._sessions = asyncio.Semaphore(self._max_concurrency)
        self._guard = guard or UrlGuard()
        # Politeness: earliest monotonic time each host may be hit again.
        self._per_host_delay = max(per_host_delay, 0.0)
        self._host_gate: dict[str, float] = {}

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    async def start(self) -> None:
        # Double-checked under a lock: two concurrent first requests would
        # otherwise each start a browser pool, and the first would leak.
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            await self._crawler.start()
            self._started = True

    async def close(self) -> None:
        async with self._start_lock:
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
        config = self._run_config(req)
        urls = [str(u) for u in req.urls]

        allowed, rejected = await self._guard.partition(urls)
        if not allowed:
            # Nothing left to do: surface it as a client error rather than a
            # 200 full of failures.
            raise BlockedUrlError(
                "; ".join(f"{url} ({reason})" for url, reason in rejected.items())
            )

        await self.start()
        pages = await asyncio.gather(
            *(self._crawl_one(url, config, req) for url in allowed)
        )
        by_url = dict(zip(allowed, pages, strict=True))
        # Preserve the caller's order and answer for every URL, including blocked ones.
        return [
            by_url[url]
            if url in by_url
            else _blocked_page(url, rejected.get(url, "no result returned by the crawler"))
            for url in urls
        ]

    async def _crawl_one(
        self, url: str, config: CrawlerRunConfig, req: CrawlRequest
    ) -> CrawledPage:
        await self._await_host_slot(url)
        async with self._sessions:
            try:
                result = await self._crawler.arun(url=url, config=config)
            except Exception as exc:  # one bad URL must not sink the whole batch
                return CrawledPage(url=url, success=False, error=f"crawl failed: {exc}")

        page = self._to_page(
            url,
            result,
            content_filter=req.content_filter,
            include_raw=req.include_raw_markdown,
        )
        return await self._guard_landing(url, page)

    async def crawl_site(
        self, req: SiteCrawlRequest, emit: Emit | None = None
    ) -> list[CrawledPage]:
        """Deep-crawl a whole site (BFS), streaming pages as they are discovered."""
        await self._guard.check(str(req.url))
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
        # A deep crawl drives its own link expansion, so it counts as one session
        # against the global cap for its whole duration.
        async with self._sessions:
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
        url = getattr(result, "url", str(req.url))
        page = self._to_page(url, result, content_filter=req.content_filter)
        # Links discovered mid-crawl were never validated by the caller: a page on a
        # public site can link straight at an internal host.
        page = await self._guard_landing(url, page)
        if emit is not None:
            await emit({"stage": "crawled", "pages": count, "url": page.url})
        return page

    async def _guard_landing(self, requested: str, page: CrawledPage) -> CrawledPage:
        """Re-check the URL the crawl actually landed on (redirects, DNS rebinding)."""
        landed = page.url or requested
        if landed == requested:
            return page
        try:
            await self._guard.check(landed)
        except BlockedUrlError as exc:
            return _blocked_page(requested, f"redirected to a blocked URL: {exc}")
        return page

    async def _await_host_slot(self, url: str) -> None:
        """Space out consecutive hits on the same host without delaying distinct ones.

        crawl4ai's dispatcher applies a 1–3 s delay to every URL in a batch, even
        when they are all on different hosts — which is the common case for search
        results. This only waits when the same host was just fetched.
        """
        if self._per_host_delay <= 0:
            return
        try:
            host, _ = self._guard.check_syntax(url)
        except BlockedUrlError:  # pragma: no cover - already filtered upstream
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        ready_at = self._host_gate.get(host, 0.0)
        # Reserve the slot before awaiting so concurrent callers queue behind us.
        self._host_gate[host] = max(now, ready_at) + self._per_host_delay
        if len(self._host_gate) > _HOST_GATE_HIGH_WATER:
            self._host_gate = {h: t for h, t in self._host_gate.items() if t > now}
        if ready_at > now:
            await asyncio.sleep(ready_at - now)

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


def _blocked_page(url: str, reason: str) -> CrawledPage:
    return CrawledPage(url=url, success=False, error=f"blocked: {reason}")


def _extract_markdown(md: object) -> tuple[str, str]:
    """Return (raw_markdown, fit_markdown). ``md`` may be a string or an object."""
    if md is None:
        return "", ""
    if isinstance(md, str):
        return md, ""
    raw = getattr(md, "raw_markdown", "") or ""
    fit = getattr(md, "fit_markdown", "") or ""
    return raw, fit
