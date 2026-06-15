from types import SimpleNamespace

from starlette.testclient import TestClient

from app.api.deps import get_crawler
from app.core.crawl.crawler import CrawlerService, _extract_markdown
from app.main import app
from app.models.crawl import CrawledPage, CrawlRequest


class FakeCrawler:
    async def crawl(self, req: CrawlRequest) -> list[CrawledPage]:
        return [
            CrawledPage(
                url=str(u),
                success=True,
                status_code=200,
                title="Example",
                markdown="# Hello\nworld",
                word_count=2,
                internal_links=3,
                external_links=1,
            )
            for u in req.urls
        ]


def test_crawl_endpoint_returns_pages() -> None:
    app.dependency_overrides[get_crawler] = lambda: FakeCrawler()
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/crawl",
                json={"urls": ["https://example.com", "https://example.org"]},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["results"][0]["title"] == "Example"
        assert body["results"][0]["markdown"].startswith("# Hello")
    finally:
        app.dependency_overrides.clear()


def test_crawl_rejects_empty_urls() -> None:
    with TestClient(app) as client:
        resp = client.post("/v1/crawl", json={"urls": []})
    assert resp.status_code == 422


def test_extract_markdown_handles_string_and_object() -> None:
    assert _extract_markdown("raw text") == ("raw text", "")
    assert _extract_markdown(None) == ("", "")
    md_obj = SimpleNamespace(raw_markdown="RAW", fit_markdown="FIT")
    assert _extract_markdown(md_obj) == ("RAW", "FIT")


def test_to_page_prefers_fit_markdown_and_counts_links() -> None:
    result = SimpleNamespace(
        success=True,
        url="https://example.com",
        status_code=200,
        markdown=SimpleNamespace(raw_markdown="raw long text", fit_markdown="fit text"),
        metadata={"title": "Title"},
        links={"internal": [1, 2], "external": [3]},
    )
    page = CrawlerService._to_page("https://example.com", result, content_filter="pruning")
    assert page.success
    assert page.markdown == "fit text"
    assert page.title == "Title"
    assert page.internal_links == 2
    assert page.external_links == 1


def test_to_page_failure() -> None:
    result = SimpleNamespace(success=False, url="https://x.com", error_message="boom")
    page = CrawlerService._to_page("https://x.com", result)
    assert page.success is False
    assert page.error == "boom"
