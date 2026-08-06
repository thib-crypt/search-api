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
import base64
import io
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
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

from app.core import metrics
from app.core.net.guard import BlockedUrlError, UrlGuard
from app.jobs.manager import Emit
from app.models.crawl import CrawledPage, CrawlRequest, SiteCrawlRequest

# Above this many tracked hosts, expired politeness entries are swept.
_HOST_GATE_HIGH_WATER = 2048

# PDF ingestion (§5.4). Redirects are followed by hand so the guard sees every hop.
_PDF_MAX_REDIRECTS = 3
_PDF_CHUNK = 64 * 1024


class _NotAPdf(Exception):
    """The candidate URL did not actually serve a PDF; fall back to the browser."""


class _PdfTooLarge(Exception):
    """The PDF exceeds CRAWL_PDF_MAX_MB. Reported as a clean per-page failure."""


def _looks_like_pdf_url(url: str) -> bool:
    """Cheap, request-free pre-filter for the PDF path.

    Deliberately *not* a HEAD probe on every URL: that would add a round-trip to
    every crawl to catch a minority of cases. The suffix covers plain ``.pdf``
    links and the ``/pdf/`` segment covers arXiv-style extensionless URLs; a
    candidate that turns out not to be a PDF simply falls back to the browser.
    """
    path = urlsplit(url).path.lower()
    return path.endswith(".pdf") or "/pdf/" in path


class CrawlerService:
    def __init__(
        self,
        max_concurrency: int = 5,
        timeout: float = 30.0,
        *,
        guard: UrlGuard | None = None,
        per_host_delay: float = 1.0,
        enable_pdf: bool = True,
        pdf_max_mb: float = 25.0,
    ) -> None:
        self._max_concurrency = max(max_concurrency, 1)
        self._timeout = timeout
        self._timeout_ms = int(timeout * 1000)
        self._pdf_enabled = enable_pdf
        self._pdf_max_bytes = int(max(pdf_max_mb, 0.0) * 1024 * 1024)
        self._http: httpx.AsyncClient | None = None
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

    @asynccontextmanager
    async def _browser_session(self) -> AsyncIterator[None]:
        """Hold one slot in the concurrency pool, counted by a live gauge (§7.2).

        ``asyncio.Semaphore`` does not expose how many permits are currently
        held, and "how many browser sessions are open right now" is not
        recoverable after the fact — so it is tracked explicitly here, in the one
        place every browser session is acquired.
        """
        async with self._sessions:
            metrics.browser_session_started()
            try:
                yield
            finally:
                metrics.browser_session_finished()

    @property
    def started(self) -> bool:
        """Whether the browser pool has actually been brought up.

        Read by `/health/ready`, which must never *start* a pool just to answer a
        probe: the pool comes up lazily on the first crawl, so "not started yet"
        is a normal state, not a failure.
        """
        return self._started

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
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _run_config(
        self, req: CrawlRequest, *, extraction_strategy: object | None = None
    ) -> CrawlerRunConfig:
        content_filter = (
            PruningContentFilter(threshold=req.prune_threshold, threshold_type="dynamic")
            if req.content_filter == "pruning"
            else None
        )
        md_generator = DefaultMarkdownGenerator(
            content_filter=content_filter,
            options={"ignore_links": req.ignore_links},
        )
        formats = set(req.formats)
        return CrawlerRunConfig(
            markdown_generator=md_generator,
            cache_mode=CacheMode.ENABLED if req.cache else CacheMode.BYPASS,
            word_count_threshold=req.word_count_threshold,
            exclude_external_links=req.exclude_external_links,
            page_timeout=self._timeout_ms,
            stream=False,
            # Both cost extra browser work, so they are only turned on when asked for.
            screenshot="screenshot" in formats,
            pdf="pdf" in formats,
            extraction_strategy=extraction_strategy,
        )

    async def crawl(self, req: CrawlRequest) -> list[CrawledPage]:
        return [page for page, _ in await self._crawl_all(req)]

    async def crawl_extracted(
        self, req: CrawlRequest, *, extraction_strategy: object
    ) -> list[tuple[CrawledPage, str | None]]:
        """Crawl with a crawl4ai extraction strategy attached.

        Returns ``(page, extracted_content)`` pairs so the structured-extraction
        endpoint can read the strategy's output without ``CrawledPage`` growing a
        field that means nothing to `/v1/crawl`.
        """
        return await self._crawl_all(req, extraction_strategy=extraction_strategy)

    async def _crawl_all(
        self, req: CrawlRequest, *, extraction_strategy: object | None = None
    ) -> list[tuple[CrawledPage, str | None]]:
        config = self._run_config(req, extraction_strategy=extraction_strategy)
        urls = [str(u) for u in req.urls]

        allowed, rejected = await self._guard.partition(urls)
        if not allowed:
            # Nothing left to do: surface it as a client error rather than a
            # 200 full of failures.
            raise BlockedUrlError(
                "; ".join(f"{url} ({reason})" for url, reason in rejected.items())
            )

        await self.start()
        pages = await asyncio.gather(*(self._crawl_one(url, config, req) for url in allowed))
        by_url = dict(zip(allowed, pages, strict=True))
        # Preserve the caller's order and answer for every URL, including blocked ones.
        results = [
            by_url[url]
            if url in by_url
            else (
                _blocked_page(url, rejected.get(url, "no result returned by the crawler")),
                None,
            )
            for url in urls
        ]
        # Counted here rather than in `_to_page`: this is the one point every page
        # the caller will see passes through, PDFs and blocked URLs included.
        metrics.record_crawled_pages([page for page, _ in results])
        return results

    async def _crawl_one(
        self, url: str, config: CrawlerRunConfig, req: CrawlRequest
    ) -> tuple[CrawledPage, str | None]:
        if self._pdf_enabled and _looks_like_pdf_url(url):
            pdf_page = await self._crawl_pdf(url, req)
            if pdf_page is not None:  # None = not a PDF after all, use the browser
                return pdf_page, None

        await self._await_host_slot(url)
        async with self._browser_session():
            try:
                result = await self._crawler.arun(url=url, config=config)
            except Exception as exc:  # one bad URL must not sink the whole batch
                return CrawledPage(url=url, success=False, error=f"crawl failed: {exc}"), None

        page = self._to_page(
            url,
            result,
            content_filter=req.content_filter,
            include_raw=req.include_raw_markdown,
            formats=req.formats,
        )
        extracted = getattr(result, "extracted_content", None)
        return await self._guard_landing(url, page), extracted

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
        async with self._browser_session():
            container = await self._crawler.arun(url=str(req.url), config=config)
            if hasattr(container, "__aiter__"):
                async for result in container:
                    pages.append(await self._collect(result, req, emit, len(pages) + 1))
            else:  # non-streaming fallback
                for result in container:
                    pages.append(await self._collect(result, req, emit, len(pages) + 1))
        metrics.record_crawled_pages(pages)
        return pages

    async def _collect(
        self, result: object, req: SiteCrawlRequest, emit: Emit | None, count: int
    ) -> CrawledPage:
        url = getattr(result, "url", str(req.url))
        page = self._to_page(url, result, content_filter=req.content_filter)
        # Links discovered mid-crawl were never validated by the caller: a page on a
        # public site can link straight at an internal host.
        page = await self._guard_landing(url, page, requested_was_checked=False)
        if emit is not None:
            await emit({"stage": "crawled", "pages": count, "url": page.url})
        return page

    async def _guard_landing(
        self, requested: str, page: CrawledPage, *, requested_was_checked: bool = True
    ) -> CrawledPage:
        """Re-check the URL the crawl actually landed on (redirects, DNS rebinding).

        The ``landed == requested`` shortcut is only sound when ``requested`` was
        itself checked before the fetch, which is the case for `crawl()`. Pages a
        deep crawl discovered on its own were never vetted by anyone, so landing
        exactly where it aimed proves nothing and the check has to run anyway --
        hence ``requested_was_checked=False`` from `_collect`.
        """
        landed = page.url or requested
        if requested_was_checked and landed == requested:
            return page
        try:
            await self._guard.check(landed)
        except BlockedUrlError as exc:
            reason = (
                f"redirected to a blocked URL: {exc}"
                if requested_was_checked
                else f"blocked: {exc}"
            )
            return _blocked_page(requested, reason)
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
        formats: Sequence[str] | None = None,
    ) -> CrawledPage:
        if not getattr(result, "success", False):
            return CrawledPage(
                url=getattr(result, "url", url),
                success=False,
                status_code=getattr(result, "status_code", None),
                error=getattr(result, "error_message", None) or "crawl failed",
            )

        wanted = set(formats or ["markdown"])
        raw_md, fit_md = _extract_markdown(getattr(result, "markdown", None))
        chosen = fit_md if (content_filter == "pruning" and fit_md) else raw_md
        if "markdown" not in wanted:
            chosen = ""
            raw_md = ""

        metadata = getattr(result, "metadata", None) or {}
        links = getattr(result, "links", None) or {}
        if not isinstance(links, dict):
            links = {}

        page = CrawledPage(
            url=getattr(result, "url", url),
            success=True,
            status_code=getattr(result, "status_code", None),
            title=metadata.get("title") if isinstance(metadata, dict) else None,
            markdown=chosen,
            raw_markdown=raw_md if (include_raw and "markdown" in wanted) else None,
            word_count=len(chosen.split()),
            # The counts are free and part of the response since v0.1; the full
            # link map is only attached when the "links" format is requested.
            internal_links=len(links.get("internal", []) or []),
            external_links=len(links.get("external", []) or []),
        )

        if "html" in wanted:
            # `cleaned_html` is the useful one for downstream pipelines (§5.3);
            # raw `html` is the fallback when the scraper did not produce it.
            page.html = getattr(result, "cleaned_html", None) or getattr(result, "html", None)
        if "screenshot" in wanted:
            page.screenshot = getattr(result, "screenshot", None)
        if "pdf" in wanted:
            page.pdf = _as_base64(getattr(result, "pdf", None))
        if "links" in wanted:
            page.links = links
        if "tables" in wanted:
            tables = getattr(result, "tables", None)
            if tables is None and isinstance(media := getattr(result, "media", None), dict):
                # Older crawl4ai builds park tables under `media`.
                tables = media.get("tables")
            page.tables = list(tables) if tables else []

        return page

    # -- PDF ingestion (§5.4) ------------------------------------------------

    async def _crawl_pdf(self, url: str, req: CrawlRequest) -> CrawledPage | None:
        """Fetch and text-extract a PDF. ``None`` means "not a PDF, use the browser"."""
        await self._await_host_slot(url)
        async with self._sessions:  # a PDF download counts against the same budget
            try:
                data, status, landed = await self._fetch_pdf(url)
            except _NotAPdf:
                return None
            except _PdfTooLarge as exc:
                return CrawledPage(url=url, success=False, error=str(exc))
            except BlockedUrlError as exc:
                return _blocked_page(url, str(exc))
            except Exception as exc:
                return CrawledPage(url=url, success=False, error=f"pdf fetch failed: {exc}")

        try:
            # pypdf is CPU-bound and this process has a single event loop: parsing a
            # 300-page PDF inline would freeze every other request.
            title, markdown = await asyncio.to_thread(_pdf_to_markdown, data)
        except Exception as exc:
            return CrawledPage(
                url=landed, success=False, status_code=status, error=f"pdf parse failed: {exc}"
            )

        wanted = set(req.formats)
        if "markdown" not in wanted:
            markdown = ""
        return CrawledPage(
            url=landed,
            success=True,
            status_code=status,
            title=title,
            markdown=markdown,
            raw_markdown=markdown if (req.include_raw_markdown and "markdown" in wanted) else None,
            word_count=len(markdown.split()),
            # A PDF has no HTML, no screenshot and no link map; those formats stay
            # null rather than being faked.
            pdf=_as_base64(data) if "pdf" in wanted else None,
        )

    async def _fetch_pdf(self, url: str) -> tuple[bytes, int | None, str]:
        """GET ``url``, enforcing the size cap and re-guarding every redirect hop."""
        client = self._pdf_client()
        current = url
        for _ in range(_PDF_MAX_REDIRECTS + 1):
            async with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise _NotAPdf
                    current = str(resp.url.join(location))
                    # httpx does not follow redirects here on purpose: every hop
                    # goes back through the SSRF guard first.
                    await self._guard.check(current)
                    continue

                content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type and content_type != "application/pdf":
                    raise _NotAPdf
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self._pdf_max_bytes:
                    raise _PdfTooLarge(
                        f"pdf is {int(declared) / 1e6:.1f} MB, over the "
                        f"{self._pdf_max_bytes / 1e6:.0f} MB limit"
                    )

                buffer = bytearray()
                async for chunk in resp.aiter_bytes(_PDF_CHUNK):
                    buffer.extend(chunk)
                    if len(buffer) > self._pdf_max_bytes:
                        raise _PdfTooLarge(
                            f"pdf exceeds the {self._pdf_max_bytes / 1e6:.0f} MB limit"
                        )
                if not content_type and not bytes(buffer[:5]).startswith(b"%PDF-"):
                    raise _NotAPdf
                return bytes(buffer), resp.status_code, str(resp.url)
        raise _NotAPdf

    def _pdf_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,  # each hop is re-checked by the guard
                headers={"User-Agent": "unified-search-api/0.1"},
            )
        return self._http


def _blocked_page(url: str, reason: str) -> CrawledPage:
    return CrawledPage(url=url, success=False, error=f"blocked: {reason}")


def _as_base64(data: object) -> str | None:
    """crawl4ai hands back raw bytes; JSON cannot carry them."""
    if isinstance(data, bytes | bytearray):
        return base64.b64encode(bytes(data)).decode("ascii")
    if isinstance(data, str):
        return data  # already base64 (screenshots, some builds' pdf field)
    return None


def _pdf_to_markdown(data: bytes) -> tuple[str | None, str]:
    """Extract a PDF's text as markdown, one ``## Page n`` section per page."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    title = None
    try:
        meta = reader.metadata
        if meta and meta.title:
            title = str(meta.title).strip() or None
    except Exception:  # noqa: BLE001 - broken metadata must not fail the extraction
        title = None

    parts: list[str] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001 - a single unreadable page is not fatal
            text = ""
        if text:
            parts.append(f"## Page {number}\n\n{text}")
    return title, "\n\n".join(parts)


def _extract_markdown(md: object) -> tuple[str, str]:
    """Return (raw_markdown, fit_markdown). ``md`` may be a string or an object."""
    if md is None:
        return "", ""
    if isinstance(md, str):
        return md, ""
    raw = getattr(md, "raw_markdown", "") or ""
    fit = getattr(md, "fit_markdown", "") or ""
    return raw, fit
