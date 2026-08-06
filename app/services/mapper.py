"""Fast URL discovery for a site (§5.2).

Three sources are tried in order and the first one that yields URLs wins:

1. ``{origin}/sitemap.xml`` (following one level of sitemap-index nesting),
2. ``Sitemap:`` directives in ``{origin}/robots.txt``,
3. the links of the single entry page, crawled through :class:`CrawlerService`.

The point of the endpoint is a sub-second answer, so the HTTP timeout is short
and the number of nested sitemaps fetched is capped: a sitemap index listing a
thousand children must not turn one request into a thousand.

Every URL fetched here comes from the client (or from a document the client
chose), so **each one goes through the SSRF guard first** — including the nested
sitemaps a remote robots.txt points at.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import httpx
from pydantic import AnyHttpUrl

from app.core.crawl.crawler import CrawlerService
from app.core.net.guard import BlockedUrlError, UrlGuard
from app.models.crawl import CrawlRequest
from app.models.map import MapRequest, MapResponse

logger = logging.getLogger("unified_search_api.map")

# Bound on amplification: a sitemap index can list thousands of children.
_MAX_NESTED_SITEMAPS = 10
# Sitemaps get big; refuse to buffer an unbounded body for a "fast" endpoint.
_MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 2


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _local(tag: str) -> str:
    """Strip the XML namespace: sitemaps declare one, sitemap indexes another."""
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(body: str) -> tuple[list[str], list[str]]:
    """Return ``(page_urls, nested_sitemap_urls)`` from a sitemap document."""
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return [], []

    pages: list[str] = []
    nested: list[str] = []
    for child in root:
        kind = _local(child.tag)
        for node in child:
            text = (node.text or "").strip()
            if _local(node.tag) != "loc" or not text:
                continue
            target = nested if kind == "sitemap" else pages
            target.append(text)
    return pages, nested


def parse_robots_sitemaps(body: str) -> list[str]:
    out: list[str] = []
    for line in body.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "sitemap" and value.strip():
            out.append(value.strip())
    return out


class MapService:
    def __init__(self, crawler: CrawlerService, guard: UrlGuard, *, timeout: float = 5.0) -> None:
        self._crawler = crawler
        self._guard = guard
        self._timeout = timeout

    async def map_site(self, req: MapRequest) -> MapResponse:
        entry = str(req.url)
        # Raises BlockedUrlError -> 400. Nothing is fetched before this passes.
        await self._guard.check(entry)
        origin = _origin(entry)

        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,  # every hop must be re-checked, so don't auto-follow
            headers={"User-Agent": "unified-search-api/0.1"},
        ) as client:
            links = await self._from_sitemap(client, urljoin(origin + "/", "sitemap.xml"))
            if links:
                return self._respond(links, "sitemap", req)

            links = await self._from_robots(client, urljoin(origin + "/", "robots.txt"))
            if links:
                return self._respond(links, "robots", req)

        return self._respond(await self._from_page(entry), "page", req)

    # -- Sources ------------------------------------------------------------

    async def _from_sitemap(self, client: httpx.AsyncClient, url: str) -> list[str]:
        body = await self._get(client, url)
        if body is None:
            return []
        pages, nested = parse_sitemap(body)
        for child in nested[:_MAX_NESTED_SITEMAPS]:
            child_body = await self._get(client, child)
            if child_body is None:
                continue
            child_pages, _ = parse_sitemap(child_body)  # one level only, no recursion
            pages.extend(child_pages)
        return pages

    async def _from_robots(self, client: httpx.AsyncClient, url: str) -> list[str]:
        body = await self._get(client, url)
        if body is None:
            return []
        pages: list[str] = []
        for sitemap_url in parse_robots_sitemaps(body)[:_MAX_NESTED_SITEMAPS]:
            pages.extend(await self._from_sitemap(client, sitemap_url))
        return pages

    async def _from_page(self, url: str) -> list[str]:
        """Last resort: crawl the entry page and read its link map."""
        try:
            pages = await self._crawler.crawl(
                CrawlRequest(urls=[AnyHttpUrl(url)], formats=["links"], ignore_links=False)
            )
        except BlockedUrlError:  # pragma: no cover - the entry URL was checked above
            return []
        if not pages or not pages[0].success:
            return []
        links = pages[0].links or {}
        out: list[str] = []
        for group in ("internal", "external"):
            for entry in links.get(group) or []:
                href = entry.get("href") or entry.get("url") if isinstance(entry, dict) else entry
                if isinstance(href, str) and href.startswith(("http://", "https://")):
                    out.append(href)
        return out

    # -- Helpers ------------------------------------------------------------

    async def _get(self, client: httpx.AsyncClient, url: str, hops: int = 0) -> str | None:
        """Guarded GET returning the body, or ``None`` on anything unusable."""
        try:
            await self._guard.check(url)
        except BlockedUrlError as exc:
            logger.info("Map skipped a blocked document %s: %s", url, exc)
            return None
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            return None
        if resp.is_redirect:
            location = resp.headers.get("location")
            # Followed by hand, and only a couple of hops: sitemap.xml commonly
            # redirects to a CDN path, but each target is re-checked by the guard.
            if not location or hops >= _MAX_REDIRECTS:
                return None
            return await self._get(client, str(resp.url.join(location)), hops + 1)
        if resp.status_code != 200 or len(resp.content) > _MAX_DOCUMENT_BYTES:
            return None
        return resp.text

    def _respond(self, links: list[str], source: str, req: MapRequest) -> MapResponse:
        needle = (req.search or "").lower()
        seen: set[str] = set()
        kept: list[str] = []
        for link in links:
            if needle and needle not in link.lower():
                continue
            if link in seen:
                continue
            seen.add(link)
            kept.append(link)
            if len(kept) >= req.limit:
                break
        return MapResponse(links=kept, count=len(kept), source=source)  # type: ignore[arg-type]
