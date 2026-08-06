"""POST /v1/map (§5.2): sitemap, robots fallback, page fallback, SSRF guard."""

from __future__ import annotations

import httpx
import respx
from starlette.testclient import TestClient

from app.api.deps import get_crawler, get_url_guard
from app.core.net.guard import UrlGuard
from app.main import app
from app.models.crawl import CrawledPage, CrawlRequest
from app.services.mapper import parse_robots_sitemaps, parse_sitemap

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/pricing</loc></url>
  <url><loc>https://example.com/about</loc></url>
  <url><loc>https://example.com/blog/pricing-2024</loc></url>
</urlset>"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
</sitemapindex>"""


class FakeCrawler:
    """Returns one page carrying a crawl4ai-shaped link map."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def crawl(self, req: CrawlRequest) -> list[CrawledPage]:
        self.calls.append([str(u) for u in req.urls])
        return [
            CrawledPage(
                url=str(req.urls[0]),
                success=True,
                links={
                    "internal": [{"href": "https://example.com/contact", "text": "Contact"}],
                    "external": [{"href": "https://other.example/partner"}],
                },
            )
        ]


def _override(crawler=None):
    app.dependency_overrides[get_crawler] = lambda: crawler or FakeCrawler()
    app.dependency_overrides[get_url_guard] = lambda: UrlGuard(allow_private=True)


# --- Parsers ---------------------------------------------------------------


def test_parse_sitemap_separates_pages_from_nested_indexes() -> None:
    pages, nested = parse_sitemap(SITEMAP)
    assert pages == [
        "https://example.com/pricing",
        "https://example.com/about",
        "https://example.com/blog/pricing-2024",
    ]
    assert nested == []

    pages, nested = parse_sitemap(SITEMAP_INDEX)
    assert pages == []
    assert nested == ["https://example.com/sitemap-pages.xml"]


def test_parse_sitemap_tolerates_garbage() -> None:
    assert parse_sitemap("not xml at all") == ([], [])


def test_parse_robots_sitemaps() -> None:
    body = "User-agent: *\nDisallow: /admin\nSitemap: https://example.com/sm.xml\n"
    assert parse_robots_sitemaps(body) == ["https://example.com/sm.xml"]


# --- Endpoint --------------------------------------------------------------


@respx.mock
def test_map_uses_the_sitemap_when_present() -> None:
    respx.get("https://example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=SITEMAP)
    )
    _override()
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/map", json={"url": "https://example.com/anything"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "sitemap"
        assert body["count"] == 3
        assert "https://example.com/pricing" in body["links"]
    finally:
        app.dependency_overrides.clear()


@respx.mock
def test_map_follows_one_level_of_sitemap_index() -> None:
    respx.get("https://example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=SITEMAP_INDEX)
    )
    respx.get("https://example.com/sitemap-pages.xml").mock(
        return_value=httpx.Response(200, text=SITEMAP)
    )
    _override()
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/map", json={"url": "https://example.com/"})
        assert resp.json()["count"] == 3
        assert resp.json()["source"] == "sitemap"
    finally:
        app.dependency_overrides.clear()


@respx.mock
def test_map_falls_back_to_robots_txt() -> None:
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(
            200, text="User-agent: *\nSitemap: https://example.com/custom-sitemap.xml\n"
        )
    )
    respx.get("https://example.com/custom-sitemap.xml").mock(
        return_value=httpx.Response(200, text=SITEMAP)
    )
    _override()
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/map", json={"url": "https://example.com/"})
        body = resp.json()
        assert body["source"] == "robots"
        assert body["count"] == 3
    finally:
        app.dependency_overrides.clear()


@respx.mock
def test_map_falls_back_to_crawling_the_entry_page() -> None:
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
    crawler = FakeCrawler()
    _override(crawler)
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/map", json={"url": "https://example.com/"})
        body = resp.json()
        assert body["source"] == "page"
        assert set(body["links"]) == {
            "https://example.com/contact",
            "https://other.example/partner",
        }
        assert crawler.calls == [["https://example.com/"]]
    finally:
        app.dependency_overrides.clear()


@respx.mock
def test_search_filters_and_limit_truncates() -> None:
    respx.get("https://example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=SITEMAP)
    )
    _override()
    try:
        with TestClient(app) as client:
            filtered = client.post(
                "/v1/map", json={"url": "https://example.com/", "search": "PRICING"}
            ).json()
            capped = client.post("/v1/map", json={"url": "https://example.com/", "limit": 1}).json()
        # Case-insensitive substring match on the URL.
        assert filtered["count"] == 2
        assert all("pricing" in link for link in filtered["links"])
        assert capped["count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_blocked_entry_url_is_rejected_before_any_fetch() -> None:
    app.dependency_overrides[get_crawler] = lambda: FakeCrawler()
    app.dependency_overrides[get_url_guard] = lambda: UrlGuard()  # the real guard
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/map", json={"url": "http://169.254.169.254/latest/meta-data/"})
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()
