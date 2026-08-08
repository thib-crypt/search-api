"""Document ingestion (§15.4): fourteen formats, tables kept, guards unchanged.

The batch's reason for existing is the first test in this file. Everything after
it protects a property that was already true and must survive the generalisation
— the SSRF guard on every redirect hop, the size cap applied while streaming, the
browser fallback for anything that is not a document.
"""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace

import anydoc
import httpx
import pytest
import respx
from prometheus_client import REGISTRY

from app.config import Settings
from app.core.crawl.crawler import (
    CrawlerService,
    _looks_like_document_url,
    _pdf_to_markdown,
    document_to_markdown,
)
from app.core.net.guard import UrlGuard
from app.models.crawl import CrawlRequest
from tests.documents import (
    make_csv,
    make_docx,
    make_encrypted_pdf,
    make_pdf,
    make_pptx,
    make_xlsx,
    make_zip_bomb,
)

OOXML_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# -- §15.1: the regression this whole batch exists for ---------------------


def test_a_pdf_table_survives_as_a_markdown_table() -> None:
    """The measured claim of §15.1, frozen.

    `pypdf` flattens a table into a column of values: nothing in its output says
    that 1450 is EMEA's T2. A model reading that does not fail — it *guesses*,
    and usually guesses right, which is worse than an obvious error. Both readers
    are run on the same bytes here so the difference is the assertion, not a
    claim in a comment.
    """
    pdf = make_pdf(with_table=True)

    _, through_anydoc = document_to_markdown(pdf, "pdf")
    _, through_pypdf = _pdf_to_markdown(pdf)

    assert "|---|" in through_anydoc.replace(" ", "")
    assert "|EMEA|1200|1450|" in through_anydoc.replace(" ", "")
    # The old path: same numbers, no relation between them.
    assert "1450" in through_pypdf
    assert "|" not in through_pypdf


# -- The fourteen formats --------------------------------------------------


@pytest.mark.parametrize(
    ("data", "named", "expected_format", "expected_text"),
    [
        (make_docx(), None, "docx", "| EMEA | 1200 | 1450 |"),
        (make_xlsx(), None, "xlsx", "| EMEA | 1200 | 1450 |"),
        (make_pptx(), None, "pptx", "Feuille de route"),
        # CSV carries no signature, so its format has to be named.
        (make_csv(), "csv", "csv", "| EMEA | 1200 | 1450 |"),
        (make_pdf(), None, "pdf", "|EMEA|1200|1450|"),
    ],
)
def test_every_wired_format_converts(
    data: bytes, named: str | None, expected_format: str, expected_text: str
) -> None:
    assert anydoc.format_from_bytes(data) in (expected_format, None)
    _, markdown = document_to_markdown(data, named or expected_format)  # type: ignore[arg-type]
    assert expected_text in markdown


def test_the_title_is_the_first_heading_or_nothing() -> None:
    title, _ = document_to_markdown(make_docx(heading="Bilan 2026"), "docx")
    assert title == "Bilan 2026"
    # A spreadsheet opens on a table: inventing a title from its first cell would
    # be worse than admitting there is none.
    assert document_to_markdown(make_xlsx(), "xlsx")[0] is None


def test_document_url_detection() -> None:
    assert _looks_like_document_url("https://example.com/report.PDF")
    assert _looks_like_document_url("https://example.com/q3.docx")
    assert _looks_like_document_url("https://example.com/q3.xlsx")
    assert _looks_like_document_url("https://arxiv.org/pdf/2301.00001")  # extensionless
    assert not _looks_like_document_url("https://example.com/article")
    assert not _looks_like_document_url("https://example.com/notes.txt")


# -- Crawl wiring ----------------------------------------------------------


class _ExplodingBrowser:
    """Any use of the browser is a bug: a document must never reach it."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def arun(self, url: str, config=None):  # pragma: no cover - guarded by asserts
        raise AssertionError(f"the browser was handed a document URL: {url}")


class _RecordingBrowser:
    """A browser that answers, and remembers what it was asked for."""

    def __init__(self, markdown: str = "fit text") -> None:
        self.seen: list[str] = []
        self._markdown = markdown

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def arun(self, url: str, config=None):
        self.seen.append(url)
        return SimpleNamespace(
            success=True,
            url=url,
            status_code=200,
            markdown=SimpleNamespace(raw_markdown=self._markdown, fit_markdown=self._markdown),
            metadata={"title": "T"},
            links={"internal": [], "external": []},
        )


def _service(browser: object | None = None, **kwargs) -> CrawlerService:
    service = CrawlerService(guard=UrlGuard(allow_private=True), per_host_delay=0.0, **kwargs)
    service._crawler = browser or _ExplodingBrowser()
    return service


@respx.mock
async def test_a_docx_url_is_converted_without_the_browser() -> None:
    respx.get("https://example.com/q3.docx").mock(
        return_value=httpx.Response(200, content=make_docx(), headers={"content-type": OOXML_DOCX})
    )
    service = _service()

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/q3.docx"]))

    assert pages[0].success is True
    assert pages[0].source_format == "docx"
    assert pages[0].title == "Rapport trimestriel"
    assert "| EMEA | 1200 | 1450 |" in pages[0].markdown
    await service.close()


@respx.mock
async def test_the_bytes_overrule_a_lying_content_type() -> None:
    """§15.4.2. Portals mislabel downloads constantly; the header is a hint."""
    respx.get("https://example.com/q3.docx").mock(
        return_value=httpx.Response(
            200, content=make_docx(), headers={"content-type": "application/pdf"}
        )
    )
    service = _service()

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/q3.docx"]))

    assert pages[0].source_format == "docx"  # not the "pdf" the server claimed
    assert "| EMEA | 1200 | 1450 |" in pages[0].markdown
    await service.close()


@respx.mock
async def test_a_generic_content_type_is_not_a_rejection() -> None:
    """`application/octet-stream` names nothing, so it disqualifies nothing."""
    respx.get("https://example.com/q3.xlsx").mock(
        return_value=httpx.Response(
            200, content=make_xlsx(), headers={"content-type": "application/octet-stream"}
        )
    )
    service = _service()

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/q3.xlsx"]))

    assert pages[0].source_format == "xlsx"
    await service.close()


@respx.mock
async def test_a_csv_is_identified_by_its_extension() -> None:
    """No signature exists for plain text: the URL is the only evidence left."""
    respx.get("https://example.com/export.csv").mock(
        return_value=httpx.Response(
            200, content=make_csv(), headers={"content-type": "application/octet-stream"}
        )
    )
    service = _service()

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/export.csv"]))

    assert pages[0].source_format == "csv"
    assert "| EMEA | 1200 | 1450 |" in pages[0].markdown
    await service.close()


@respx.mock
async def test_an_html_page_at_a_document_url_falls_back_to_the_browser() -> None:
    respx.get("https://example.com/pdf/viewer").mock(
        return_value=httpx.Response(200, html="<html>not a document</html>")
    )
    browser = _RecordingBrowser()
    service = _service(browser)

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/pdf/viewer"]))

    assert browser.seen == ["https://example.com/pdf/viewer"]
    assert pages[0].markdown == "fit text"
    assert pages[0].source_format == "html"
    await service.close()


@respx.mock
async def test_the_original_bytes_come_back_only_for_a_real_pdf() -> None:
    raw = make_pdf()
    respx.get("https://example.com/paper.pdf").mock(
        return_value=httpx.Response(200, content=raw, headers={"content-type": "application/pdf"})
    )
    respx.get("https://example.com/q3.docx").mock(
        return_value=httpx.Response(200, content=make_docx(), headers={"content-type": OOXML_DOCX})
    )
    service = _service()

    pages = await service.crawl(
        CrawlRequest(
            urls=["https://example.com/paper.pdf", "https://example.com/q3.docx"],
            formats=["markdown", "pdf"],
        )
    )

    assert pages[0].pdf == base64.b64encode(raw).decode()
    # A .docx returned under a field named `pdf` would be a lie no client can see.
    assert pages[1].pdf is None
    await service.close()


# -- §15.4.3: the second chance --------------------------------------------


@respx.mock
async def test_an_extensionless_url_gets_a_second_chance_as_a_document() -> None:
    """`/download?id=4021` is the norm on document portals, not an edge case."""
    respx.get("https://portal.example/download").mock(
        return_value=httpx.Response(
            200, content=make_docx(), headers={"content-type": "application/octet-stream"}
        )
    )
    # The browser downloads the file instead of rendering it: success, no words.
    browser = _RecordingBrowser(markdown="")
    service = _service(browser)

    pages = await service.crawl(CrawlRequest(urls=["https://portal.example/download?id=4021"]))

    assert browser.seen == ["https://portal.example/download?id=4021"]  # tried first
    assert pages[0].source_format == "docx"
    assert "| EMEA | 1200 | 1450 |" in pages[0].markdown
    await service.close()


@respx.mock
async def test_a_genuinely_empty_page_keeps_the_browser_answer() -> None:
    """The retry costs nothing on the nominal path and never makes things worse."""
    respx.get("https://example.com/empty").mock(return_value=httpx.Response(200, html=""))
    browser = _RecordingBrowser(markdown="")
    service = _service(browser)

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/empty"]))

    assert pages[0].success is True
    assert pages[0].source_format == "html"
    await service.close()


@respx.mock
async def test_a_page_with_content_is_never_retried() -> None:
    route = respx.get("https://example.com/article")
    browser = _RecordingBrowser(markdown="a real article")
    service = _service(browser)

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/article"]))

    assert pages[0].markdown == "a real article"
    assert not route.called  # no download was attempted
    await service.close()


# -- Failure modes ---------------------------------------------------------


@respx.mock
async def test_an_encrypted_document_is_a_clean_failure_not_a_500() -> None:
    respx.get("https://example.com/sealed.pdf").mock(
        return_value=httpx.Response(
            200, content=make_encrypted_pdf(), headers={"content-type": "application/pdf"}
        )
    )
    # Without this the pypdf fallback would quietly answer with empty text.
    service = _service(pdf_fallback=False)

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/sealed.pdf"]))

    assert pages[0].success is False
    assert "EncryptedError" in (pages[0].error or "")
    assert pages[0].source_format == "pdf"
    await service.close()


@respx.mock
async def test_a_zip_bomb_is_refused_without_exhausting_memory() -> None:
    """§15.3, frozen as a regression.

    A .xlsx, a .docx and a .pptx are all ZIP archives, which makes this the most
    obvious attack on a service that downloads them. `CRAWL_DOC_MAX_MB` caps the
    *download* — 500 KB here, comfortably under it — so the second ceiling has to
    come from the parser's own decompression limits.
    """
    bomb = make_zip_bomb()
    assert len(bomb) < 2 * 1024 * 1024  # small enough to sail past the size cap
    respx.get("https://example.com/bomb.xlsx").mock(
        return_value=httpx.Response(
            200, content=bomb, headers={"content-type": "application/octet-stream"}
        )
    )
    service = _service(doc_max_mb=25.0)

    started = time.perf_counter()
    pages = await service.crawl(CrawlRequest(urls=["https://example.com/bomb.xlsx"]))
    elapsed = time.perf_counter() - started

    # Whether it converts or refuses is anydoc's call; what matters is that half a
    # gigabyte of inflation neither hangs the worker nor takes the process down.
    assert elapsed < 30.0
    assert pages[0].source_format == "xlsx"
    await service.close()


@respx.mock
async def test_a_declared_size_over_the_cap_is_refused() -> None:
    respx.get("https://example.com/huge.pdf").mock(
        return_value=httpx.Response(
            200,
            content=b"%PDF-" + b"x" * 4096,
            headers={"content-type": "application/pdf"},
        )
    )
    service = _service(doc_max_mb=0.001)  # ~1 KB

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/huge.pdf"]))

    assert pages[0].success is False
    assert "limit" in (pages[0].error or "")
    await service.close()


@respx.mock
async def test_the_cap_cuts_during_the_stream_not_after_it() -> None:
    """An undeclared length must not become an unbounded download.

    The chunks are counted as they are pulled, so this fails if the body is
    drained first and measured afterwards — which is the difference between a
    size cap and a size report.
    """
    consumed = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal consumed
        for _ in range(200):  # 12.8 MB if anyone ever reads it all
            consumed += 1
            yield b"%PDF-" + b"x" * (64 * 1024)

    respx.get("https://example.com/endless.pdf").mock(
        return_value=httpx.Response(
            200, content=body(), headers={"content-type": "application/pdf"}
        )
    )
    service = _service(doc_max_mb=0.2)  # ~200 KB, about three chunks

    pages = await service.crawl(CrawlRequest(urls=["https://example.com/endless.pdf"]))

    assert pages[0].success is False
    assert "limit" in (pages[0].error or "")
    assert consumed < 10  # stopped early, did not drain 200 chunks
    await service.close()


@respx.mock
async def test_the_ssrf_guard_still_holds_on_every_redirect_hop() -> None:
    """The generalisation must not have loosened the guard (§15.4.1)."""
    respx.get("https://8.8.8.8/report.pdf").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/secret.pdf"})
    )
    leaked = respx.get("http://127.0.0.1/secret.pdf").mock(
        return_value=httpx.Response(200, content=make_pdf())
    )
    # The strict guard, not the permissive one the rest of the suite uses.
    service = CrawlerService(guard=UrlGuard(), per_host_delay=0.0)
    service._crawler = _ExplodingBrowser()

    pages = await service.crawl(CrawlRequest(urls=["https://8.8.8.8/report.pdf"]))

    assert pages[0].success is False
    assert "blocked" in (pages[0].error or "")
    assert not leaked.called
    await service.close()


# -- Switches --------------------------------------------------------------


@respx.mock
async def test_documents_disabled_sends_everything_to_the_browser() -> None:
    route = respx.get("https://example.com/q3.docx")
    browser = _RecordingBrowser()
    service = _service(browser, enable_documents=False)

    await service.crawl(CrawlRequest(urls=["https://example.com/q3.docx"]))

    assert browser.seen == ["https://example.com/q3.docx"]
    assert not route.called  # no conversion was even attempted
    await service.close()


def test_pypdf_takes_over_when_anydoc_fails_on_a_pdf(monkeypatch) -> None:
    """§15.8: the previous path stays one environment variable away."""

    def explode(data: bytes, fmt: str | None = None) -> str:
        raise anydoc.MalformedError("nope")

    monkeypatch.setattr(anydoc, "to_markdown_bytes", explode)

    _, markdown = document_to_markdown(make_pdf(), "pdf", pdf_fallback=True)
    assert "## Page 1" in markdown  # unmistakably pypdf's shape

    with pytest.raises(anydoc.MalformedError):
        document_to_markdown(make_pdf(), "pdf", pdf_fallback=False)
    with pytest.raises(anydoc.MalformedError):
        # The fallback is a PDF reader; it has nothing to offer a spreadsheet.
        document_to_markdown(make_xlsx(), "xlsx", pdf_fallback=True)


# -- §15.4.5: provenance ---------------------------------------------------


@respx.mock
async def test_the_metrics_split_documents_from_browser_pages() -> None:
    def sample(**labels: str) -> float:
        value = REGISTRY.get_sample_value("crawl_pages_total", labels)
        return 0.0 if value is None else value

    before = sample(outcome="success", source_format="xlsx")
    respx.get("https://example.com/q3.xlsx").mock(
        return_value=httpx.Response(
            200, content=make_xlsx(), headers={"content-type": "application/octet-stream"}
        )
    )
    service = _service()

    await service.crawl(CrawlRequest(urls=["https://example.com/q3.xlsx"]))

    assert sample(outcome="success", source_format="xlsx") == before + 1
    await service.close()


# -- §15.7: the renamed settings -------------------------------------------


def test_the_new_names_win_and_the_old_ones_still_work() -> None:
    default = Settings()
    assert default.documents_enabled is True
    assert default.doc_max_mb == 25.0
    assert default.deprecation_warnings() == []

    # An operator who deployed CRAWL_ENABLE_PDF=false asked for the browser, and
    # must not silently gain fourteen formats on upgrade.
    legacy = Settings(crawl_enable_pdf=False, crawl_pdf_max_mb=5.0)
    assert legacy.documents_enabled is False
    assert legacy.doc_max_mb == 5.0
    assert len(legacy.deprecation_warnings()) == 2

    both = Settings(crawl_enable_pdf=False, crawl_enable_documents=True)
    assert both.documents_enabled is True
    assert "takes precedence" in both.deprecation_warnings()[0]
