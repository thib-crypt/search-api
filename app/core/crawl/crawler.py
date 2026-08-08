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

import anydoc
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

# Document ingestion (§15.4). Redirects are followed by hand so the guard sees every hop.
_DOCUMENT_MAX_REDIRECTS = 3
_DOCUMENT_CHUNK = 64 * 1024

# Extensions worth trying the document path on. Filtered through anydoc rather
# than hard-coded: the installed version decides what it can actually read, so a
# candidate this build does not support never gets routed away from the browser.
_DOCUMENT_SUFFIXES: tuple[str, ...] = tuple(
    sorted(
        f".{extension}"
        for extension in (
            "csv",
            "doc",
            "docm",
            "docx",
            "epub",
            "odp",
            "ods",
            "odt",
            "pdf",
            "pps",
            "ppsx",
            "ppt",
            "pptm",
            "pptx",
            "rtf",
            "xls",
            "xlsb",
            "xlsm",
            "xlsx",
        )
        if anydoc.format_from_extension(extension) is not None
    )
)

# Content types that name a format outright. A hint, not a verdict: §15.4.2 gives
# the last word to the bytes, because portals mislabel downloads constantly.
_CONTENT_TYPE_FORMATS: dict[str, anydoc.Format] = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-word.document.macroenabled.12": "docx",
    "application/vnd.ms-excel": "xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow": "pptx",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": "pptx",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.oasis.opendocument.spreadsheet": "ods",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "application/epub+zip": "epub",
    "text/csv": "csv",
    "application/csv": "csv",
}

# Types that name nothing. Widely served by document portals, so they must not
# disqualify a URL — they simply leave the decision entirely to the bytes.
_GENERIC_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "application/x-download",
        "application/download",
        "application/force-download",
        "binary/octet-stream",
    }
)


class _NotADocument(Exception):
    """The candidate URL did not serve a document; fall back to the browser."""


class _DocumentTooLarge(Exception):
    """The document exceeds CRAWL_DOC_MAX_MB. Reported as a clean per-page failure."""


def _looks_like_document_url(url: str) -> bool:
    """Cheap, request-free pre-filter for the document path.

    Deliberately *not* a HEAD probe on every URL: that would add a round-trip to
    every crawl to catch a minority of cases. The suffixes cover plain
    ``.pdf``/``.docx``/``.xlsx`` links and the ``/pdf/`` segment covers
    arXiv-style extensionless URLs; a candidate that turns out not to be a
    document simply falls back to the browser. Extensionless URLs the suffix
    list cannot see (``/download?id=4021``) are caught after the fact by the
    second chance in `_retry_as_document`.
    """
    path = urlsplit(url).path.lower()
    return path.endswith(_DOCUMENT_SUFFIXES) or "/pdf/" in path


def _format_from_content_type(content_type: str) -> anydoc.Format | None:
    return _CONTENT_TYPE_FORMATS.get(content_type)


def _format_from_url(url: str) -> anydoc.Format | None:
    """The format the URL's own extension names, if any.

    Last resort behind the bytes and the content type, and the only thing that
    can identify a CSV served as ``application/octet-stream``: plain-text formats
    carry no signature, so `format_from_bytes` can never recognize them.
    """
    return anydoc.format_from_path(urlsplit(url).path)


class CrawlerService:
    def __init__(
        self,
        max_concurrency: int = 5,
        timeout: float = 30.0,
        *,
        guard: UrlGuard | None = None,
        per_host_delay: float = 1.0,
        enable_documents: bool = True,
        doc_max_mb: float = 25.0,
        pdf_fallback: bool = True,
    ) -> None:
        self._max_concurrency = max(max_concurrency, 1)
        self._timeout = timeout
        self._timeout_ms = int(timeout * 1000)
        self._documents_enabled = enable_documents
        self._doc_max_bytes = int(max(doc_max_mb, 0.0) * 1024 * 1024)
        self._pdf_fallback = pdf_fallback
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
        tried_document = self._documents_enabled and _looks_like_document_url(url)
        if tried_document:
            document = await self._crawl_document(url, req)
            if document is not None:  # None = not a document after all, use the browser
                return document, None

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
        page = await self._guard_landing(url, page)
        if not tried_document:
            page = await self._retry_as_document(url, page, req)
        return page, extracted

    async def _retry_as_document(
        self, url: str, page: CrawledPage, req: CrawlRequest
    ) -> CrawledPage:
        """Second chance for a document URL the suffix filter could not see (§15.4.3).

        A ``/download?id=4021`` that serves a .docx comes back from the browser as
        a success with nothing in it — ``success: true``, ``markdown: ""``,
        ``word_count: 0`` — which no caller can tell apart from a genuinely empty
        page. Only that exact outcome triggers the retry, so the nominal path pays
        nothing for it. A retry that also fails is not news: the browser's answer
        is kept rather than replaced by a second, differently-worded failure.
        """
        if not self._documents_enabled or "markdown" not in set(req.formats):
            return page
        if not page.success or page.word_count:
            return page
        document = await self._crawl_document(url, req)
        return document if document is not None and document.success else page

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
            source_format="html",
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

    # -- Document ingestion (§15.4) ------------------------------------------

    async def _crawl_document(self, url: str, req: CrawlRequest) -> CrawledPage | None:
        """Fetch and convert a document. ``None`` means "not a document, use the browser"."""
        await self._await_host_slot(url)
        async with self._sessions:  # a document download counts against the same budget
            try:
                data, status, landed, fmt = await self._fetch_document(url)
            except _NotADocument:
                return None
            except _DocumentTooLarge as exc:
                return CrawledPage(url=url, success=False, error=str(exc))
            except BlockedUrlError as exc:
                return _blocked_page(url, str(exc))
            except Exception as exc:
                return CrawledPage(url=url, success=False, error=f"document fetch failed: {exc}")

        try:
            # Conversion is CPU-bound and this process has a single event loop:
            # parsing a 300-page PDF inline would freeze every other request.
            # anydoc releases the GIL while it works, so unlike pypdf this thread
            # runs genuinely in parallel rather than taking turns.
            title, markdown = await asyncio.to_thread(
                document_to_markdown, data, fmt, pdf_fallback=self._pdf_fallback
            )
        except Exception as exc:
            return CrawledPage(
                url=landed,
                success=False,
                status_code=status,
                source_format=fmt,
                error=f"document parse failed: {convert_error_reason(exc)}",
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
            source_format=fmt,
            # A document has no HTML, no screenshot and no link map; those formats
            # stay null rather than being faked. The raw bytes only come back under
            # `pdf` when they actually are one — a .docx returned in a field named
            # `pdf` would be a lie the client cannot detect.
            pdf=_as_base64(data) if ("pdf" in wanted and fmt == "pdf") else None,
        )

    async def _fetch_document(self, url: str) -> tuple[bytes, int | None, str, anydoc.Format]:
        """GET ``url``, enforcing the size cap and re-guarding every redirect hop.

        Also decides *what* was fetched (§15.4.2). The content type is taken as a
        hint and the bytes as the verdict, in that order of authority: a portal
        serving a real .docx as ``application/octet-stream`` is read correctly,
        and an HTML error page served as ``application/pdf`` is not.
        """
        client = self._document_client()
        current = url
        for _ in range(_DOCUMENT_MAX_REDIRECTS + 1):
            async with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise _NotADocument
                    current = str(resp.url.join(location))
                    # httpx does not follow redirects here on purpose: every hop
                    # goes back through the SSRF guard first.
                    await self._guard.check(current)
                    continue

                content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                hinted = _format_from_content_type(content_type)
                if content_type and hinted is None and content_type not in _GENERIC_CONTENT_TYPES:
                    # A type that names something else entirely (text/html,
                    # image/png): no point downloading it to find that out.
                    raise _NotADocument
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self._doc_max_bytes:
                    raise _DocumentTooLarge(
                        f"document is {int(declared) / 1e6:.1f} MB, over the "
                        f"{self._doc_max_bytes / 1e6:.0f} MB limit"
                    )

                buffer = bytearray()
                async for chunk in resp.aiter_bytes(_DOCUMENT_CHUNK):
                    buffer.extend(chunk)
                    if len(buffer) > self._doc_max_bytes:
                        raise _DocumentTooLarge(
                            f"document exceeds the {self._doc_max_bytes / 1e6:.0f} MB limit"
                        )
                data = bytes(buffer)
                # The bytes have the last word; the header and the URL only break
                # the tie for the signature-less formats (CSV).
                fmt = anydoc.format_from_bytes(data) or hinted or _format_from_url(str(resp.url))
                if fmt is None:
                    raise _NotADocument
                return data, resp.status_code, str(resp.url), fmt
        raise _NotADocument

    def _document_client(self) -> httpx.AsyncClient:
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


def convert_error_reason(exc: BaseException) -> str:
    """A caller-readable one-liner for a failed conversion.

    The exception *type* carries most of the information anydoc has — a
    password-protected file and a truncated one are different problems with
    different fixes — so it is named rather than flattened into the message.
    """
    return f"{type(exc).__name__}: {exc}"


def document_to_markdown(
    data: bytes, fmt: anydoc.Format, *, pdf_fallback: bool = True
) -> tuple[str | None, str]:
    """Convert ``data`` to Markdown, returning ``(title, markdown)``.

    Blocking, and meant to be run on a worker thread (see the call sites):
    anydoc releases the GIL for the duration of the conversion, so this is real
    parallelism rather than two threads taking turns.
    """
    try:
        markdown = anydoc.to_markdown_bytes(data, fmt)
    except Exception:
        # §15.8: the previous extraction path stays one environment variable away
        # while anydoc is still 0.1.x. Catching `Exception` rather than
        # `ConvertError` on purpose — if a young parser fails in a way we did not
        # anticipate, degraded text still beats no page at all.
        if fmt != "pdf" or not pdf_fallback:
            raise
        return _pdf_to_markdown(data)
    return _title_from_markdown(markdown), markdown


def _title_from_markdown(markdown: str) -> str | None:
    """The document's first heading, or ``None`` when it opens without one.

    anydoc returns rendered Markdown and nothing else — there is no title field
    to read. The first heading is what a reader would call the title in every one
    of the supported formats, and a document that starts with body text or a
    table is left untitled rather than given an invented one.
    """
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title[:300]
        elif stripped:
            break  # body text before any heading: nothing here is a title
    return None


def _pdf_to_markdown(data: bytes) -> tuple[str | None, str]:
    """Extract a PDF's text as markdown, one ``## Page n`` section per page.

    The pre-Phase-15 path, kept as the CRAWL_PDF_FALLBACK safety net (§15.8).
    Tables come out of this as a bare column of values — which is the regression
    anydoc exists to fix — so it is a fallback, never a first choice.
    """
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
