"""`POST /v1/convert` (§15.5): a file in, Markdown out.

The one endpoint that fetches nothing, so the SSRF guard has nothing to do here
— which is exactly why its two other limits (the size cap and the 400-not-500
rule for unreadable files) are worth pinning down.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.api.middleware import path_weight
from app.config import get_settings
from app.main import app
from tests.documents import (
    make_csv,
    make_docx,
    make_encrypted_pdf,
    make_pdf,
    make_xlsx,
)

OOXML_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def client() -> TestClient:
    # No context manager: this endpoint touches none of the clients the lifespan
    # builds, and starting a browser pool to convert a byte string is absurd.
    return TestClient(app)


def _post(client: TestClient, name: str, data: bytes, **form: str):
    return client.post(
        "/v1/convert",
        files={"file": (name, data, "application/octet-stream")},
        data=form or None,
    )


def test_a_docx_converts_to_markdown(client: TestClient) -> None:
    resp = _post(client, "q3.docx", make_docx())

    assert resp.status_code == 200
    body = resp.json()
    assert body["format"] == "docx"
    assert body["title"] == "Rapport trimestriel"
    assert "| EMEA | 1200 | 1450 |" in body["markdown"]
    assert body["word_count"] == len(body["markdown"].split())


def test_a_pdf_keeps_its_tables(client: TestClient) -> None:
    body = _post(client, "report.pdf", make_pdf()).json()

    assert body["format"] == "pdf"
    assert "|EMEA|1200|1450|" in body["markdown"].replace(" ", "")


def test_a_spreadsheet_has_no_title(client: TestClient) -> None:
    body = _post(client, "q3.xlsx", make_xlsx()).json()

    assert body["format"] == "xlsx"
    assert body["title"] is None


def test_a_csv_needs_its_format_named(client: TestClient) -> None:
    """Plain text carries no signature — this is the case `format` exists for."""
    named = _post(client, "export.csv", make_csv(), format="csv").json()
    assert named["format"] == "csv"
    assert "| EMEA | 1200 | 1450 |" in named["markdown"]

    # The filename is the fallback when the field is omitted...
    assert _post(client, "export.csv", make_csv()).json()["format"] == "csv"
    # ...and with neither, there is genuinely nothing left to go on.
    resp = _post(client, "export", make_csv())
    assert resp.status_code == 400
    assert "format" in resp.json()["detail"]


def test_the_bytes_overrule_a_wrong_format_field(client: TestClient) -> None:
    """§15.4.2's rule, applied here too: the bytes have the last word.

    Worth pinning because the alternative fails silently. anydoc converts
    whatever it is told to convert, so honouring `format=csv` on a .docx would
    return a 200 carrying a page of mojibake — a wrong answer the caller has no
    way to detect. Detection is the signal that cannot be wrong by accident.
    """
    body = _post(client, "q3.docx", make_docx(), format="csv").json()

    assert body["format"] == "docx"
    assert "| EMEA | 1200 | 1450 |" in body["markdown"]


def test_an_unknown_format_is_refused_with_the_supported_list(client: TestClient) -> None:
    resp = _post(client, "notes.txt", b"hello", format="txt")

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "txt" in detail
    assert "docx" in detail and "pdf" in detail


def test_an_unrecognizable_file_is_a_400_not_a_500(client: TestClient) -> None:
    resp = _post(client, "mystery.bin", b"\x00\x01\x02 not a document")

    assert resp.status_code == 400


def test_an_encrypted_document_names_the_reason(client: TestClient) -> None:
    """A password-protected file and a truncated one need different fixes."""
    resp = _post(client, "sealed.pdf", make_encrypted_pdf())

    assert resp.status_code == 400
    assert "EncryptedError" in resp.json()["detail"]


def test_an_oversized_upload_is_refused(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("CONVERT_MAX_MB", "0.001")  # ~1 KB
    get_settings.cache_clear()

    resp = _post(client, "q3.docx", make_docx())

    assert resp.status_code == 413
    assert "limit" in resp.json()["detail"].lower()


def test_convert_is_priced_like_map_not_like_a_crawl() -> None:
    """§15.5: no LLM call, no browser session — one conversion on a worker thread."""
    assert path_weight("/v1/convert") == path_weight("/v1/map") == 2
    assert path_weight("/v1/convert") < path_weight("/v1/crawl")


def test_the_endpoint_is_advertised(client: TestClient) -> None:
    assert "/v1/convert" in client.get("/").json()["endpoints"]
