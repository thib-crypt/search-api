"""Crawl output formats (§5.3) and PDF ingestion (§5.4)."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import respx

from app.core.crawl.crawler import CrawlerService, _looks_like_pdf_url, _pdf_to_markdown
from app.core.net.guard import UrlGuard
from app.models.crawl import CrawlRequest


def _rich_result(url: str = "https://example.com"):
    return SimpleNamespace(
        success=True,
        url=url,
        status_code=200,
        markdown=SimpleNamespace(raw_markdown="raw text", fit_markdown="fit text"),
        metadata={"title": "T"},
        links={"internal": ["https://example.com/a"], "external": ["https://other.example/b"]},
        html="<html>raw</html>",
        cleaned_html="<article>clean</article>",
        screenshot="c2NyZWVuc2hvdA==",
        pdf=b"%PDF-1.4 fake",
        tables=[{"headers": ["a"], "rows": [["1"]]}],
    )


def test_default_formats_return_markdown_only() -> None:
    page = CrawlerService._to_page("https://example.com", _rich_result())

    assert page.markdown == "fit text"
    assert page.html is None
    assert page.screenshot is None
    assert page.pdf is None
    assert page.links is None
    assert page.tables is None
    # The link counts predate §5.3 and stay in the response unconditionally.
    assert page.internal_links == 1
    assert page.external_links == 1


def test_each_requested_format_populates_its_field() -> None:
    page = CrawlerService._to_page(
        "https://example.com",
        _rich_result(),
        formats=["markdown", "html", "screenshot", "pdf", "links", "tables"],
    )

    assert page.markdown == "fit text"
    assert page.html == "<article>clean</article>"  # cleaned_html wins over raw html
    assert page.screenshot == "c2NyZWVuc2hvdA=="
    assert page.pdf == base64.b64encode(b"%PDF-1.4 fake").decode()
    assert page.links == {
        "internal": ["https://example.com/a"],
        "external": ["https://other.example/b"],
    }
    assert page.tables == [{"headers": ["a"], "rows": [["1"]]}]


def test_markdown_is_dropped_when_not_requested() -> None:
    page = CrawlerService._to_page(
        "https://example.com", _rich_result(), formats=["screenshot"], include_raw=True
    )

    assert page.markdown == ""
    assert page.raw_markdown is None
    assert page.word_count == 0
    assert page.screenshot == "c2NyZWVuc2hvdA=="


def test_run_config_only_turns_on_what_was_asked_for() -> None:
    service = CrawlerService(guard=UrlGuard(allow_private=True), per_host_delay=0.0)

    plain = service._run_config(CrawlRequest(urls=["https://example.com"]))
    assert plain.screenshot is False
    assert plain.pdf is False

    rich = service._run_config(
        CrawlRequest(urls=["https://example.com"], formats=["screenshot", "pdf"])
    )
    assert rich.screenshot is True
    assert rich.pdf is True


def test_html_format_falls_back_to_raw_html() -> None:
    result = _rich_result()
    result.cleaned_html = None
    page = CrawlerService._to_page("https://example.com", result, formats=["html"])
    assert page.html == "<html>raw</html>"


# --- PDF ingestion --------------------------------------------------------


def _minimal_pdf(text: str = "Hello PDF") -> bytes:
    """A hand-rolled, valid single-page PDF containing ``text``."""
    stream = f"BT /F1 24 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n".encode()
    out += b"%%EOF\n"
    return bytes(out)


def test_pdf_url_detection() -> None:
    assert _looks_like_pdf_url("https://example.com/report.PDF")
    assert _looks_like_pdf_url("https://arxiv.org/pdf/2301.00001")  # extensionless
    assert not _looks_like_pdf_url("https://example.com/article")


def test_pdf_to_markdown_extracts_page_sections() -> None:
    title, markdown = _pdf_to_markdown(_minimal_pdf())
    assert "## Page 1" in markdown
    assert "Hello PDF" in markdown
    assert title is None or isinstance(title, str)


class _ExplodingBrowser:
    """Any use of the browser is a bug: a PDF must never reach it."""

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def arun(self, url: str, config=None):  # pragma: no cover - guarded by asserts
        raise AssertionError(f"the browser was handed a PDF URL: {url}")


def _pdf_service(**kwargs) -> CrawlerService:
    service = CrawlerService(guard=UrlGuard(allow_private=True), per_host_delay=0.0, **kwargs)
    service._crawler = _ExplodingBrowser()
    return service


@respx.mock
async def test_pdf_url_is_extracted_without_the_browser() -> None:
    respx.get("https://example.com/paper.pdf").mock(
        return_value=httpx.Response(
            200,
            content=_minimal_pdf("Quarterly results"),
            headers={"content-type": "application/pdf"},
        )
    )
    service = _pdf_service()

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/paper.pdf"]))

    assert pages[0].success is True
    assert "Quarterly results" in pages[0].markdown
    assert pages[0].word_count > 0
    await service.close()


@respx.mock
async def test_pdf_format_returns_the_original_bytes_base64() -> None:
    raw = _minimal_pdf()
    respx.get("https://example.com/paper.pdf").mock(
        return_value=httpx.Response(200, content=raw, headers={"content-type": "application/pdf"})
    )
    service = _pdf_service()

    pages = await service.crawl(
        CrawlRequest(urls=["https://example.com/paper.pdf"], formats=["markdown", "pdf"])
    )

    assert pages[0].pdf == base64.b64encode(raw).decode()
    await service.close()


@respx.mock
async def test_oversized_pdf_is_a_clean_failure() -> None:
    respx.get("https://example.com/huge.pdf").mock(
        return_value=httpx.Response(
            200,
            content=b"%PDF-" + b"x" * 4096,
            headers={"content-type": "application/pdf"},
        )
    )
    service = _pdf_service(pdf_max_mb=0.001)  # ~1 KB

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/huge.pdf"]))

    assert pages[0].success is False
    assert "limit" in (pages[0].error or "")
    await service.close()


@respx.mock
async def test_non_pdf_at_a_pdf_looking_url_falls_back_to_the_browser() -> None:
    respx.get("https://example.com/pdf/viewer").mock(
        return_value=httpx.Response(200, html="<html>not a pdf</html>")
    )
    service = CrawlerService(guard=UrlGuard(allow_private=True), per_host_delay=0.0)

    class Browser:
        seen: list[str] = []

        async def start(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def arun(self, url: str, config=None):
            Browser.seen.append(url)
            return _rich_result(url)

    service._crawler = Browser()
    pages = await service.crawl(CrawlRequest(urls=["https://example.com/pdf/viewer"]))

    assert Browser.seen == ["https://example.com/pdf/viewer"]
    assert pages[0].markdown == "fit text"
    await service.close()


@respx.mock
async def test_pdf_ingestion_can_be_disabled() -> None:
    service = CrawlerService(
        guard=UrlGuard(allow_private=True), per_host_delay=0.0, enable_pdf=False
    )

    class Browser:
        seen: list[str] = []

        async def start(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def arun(self, url: str, config=None):
            Browser.seen.append(url)
            return _rich_result(url)

    service._crawler = Browser()
    await service.crawl(CrawlRequest(urls=["https://example.com/paper.pdf"]))

    assert Browser.seen == ["https://example.com/paper.pdf"]
    await service.close()
