"""Direct document → Markdown conversion (§15.5).

The one endpoint in this API that fetches nothing: the bytes come from the
client, so there is no URL to resolve and no SSRF guard to run. That is worth
stating plainly rather than leaving a reviewer to notice the absence — every
other path here validates a URL before touching it, and this one has none to
validate.
"""

from __future__ import annotations

import asyncio
from typing import get_args

import anydoc
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from app.api.deps import require_api_key
from app.config import Settings, get_settings
from app.core.crawl.crawler import convert_error_reason, document_to_markdown
from app.models.convert import ConvertResponse

router = APIRouter(prefix="/v1", tags=["convert"], dependencies=[Depends(require_api_key)])

# Read back from anydoc rather than repeated here: the list a caller is told to
# choose from is then exactly the list the installed build accepts.
SUPPORTED_FORMATS: tuple[str, ...] = tuple(get_args(anydoc.Format))

_READ_CHUNK = 64 * 1024


async def _upload_cap(request: Request, settings: Settings = Depends(get_settings)) -> int:
    """Refuse an over-sized upload on its declared length, and return the cap.

    Two gates, because neither alone is enough. This one reads
    ``Content-Length`` and rejects with the actual size in the message, which is
    what a client needs in order to fix the call; `_read_capped` then applies the
    same cap while reading, for an upload that declares no length (chunked) or
    declares the wrong one.

    What neither gate does — worth being explicit about — is stop the *transfer*.
    Starlette's multipart parser streams a file part into a
    ``SpooledTemporaryFile`` before any application code runs, and caps only
    non-file parts, so by the time this is reached the bytes have already been
    accepted and spooled to disk. `CONVERT_MAX_MB` bounds what reaches memory and
    the converter; bounding what reaches the disk is the reverse proxy's job
    (``client_max_body_size`` and its equivalents).
    """
    max_bytes = int(max(settings.convert_max_mb, 0.0) * 1024 * 1024)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"Upload is {int(declared) / 1e6:.1f} MB, over the {max_bytes / 1e6:.0f} MB limit."
            ),
        )
    return max_bytes


@router.post(
    "/convert",
    response_model=ConvertResponse,
    summary="Convert an uploaded document (Word, Excel, PowerPoint, PDF, ...) to Markdown",
)
async def convert(
    file: UploadFile = File(..., description="The document to convert."),
    format: str | None = Form(
        default=None,
        description=(
            "Name the format instead of letting it be detected. Required for CSV, "
            f"which carries no signature in its bytes. One of: {', '.join(SUPPORTED_FORMATS)}."
        ),
    ),
    max_bytes: int = Depends(_upload_cap),
) -> ConvertResponse:
    named = _named_format(format)
    data = await _read_capped(file, max_bytes)

    # Same order of authority as the crawl path (§15.4.2): the bytes have the
    # last word, and `format` only breaks the tie they cannot. That matters
    # because a mismatched format is not refused — anydoc does what it is told,
    # so reading a .docx as `csv` yields a page of mojibake with a 200 next to
    # it. Detection is the one signal that cannot be wrong by accident.
    resolved = anydoc.format_from_bytes(data) or named or _format_from_filename(file.filename)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Could not determine the document's format from its content or its "
                "filename. Send a `format` field naming one of: "
                f"{', '.join(SUPPORTED_FORMATS)}."
            ),
        )

    try:
        # Same reasoning as the crawler: conversion is CPU-bound and this process
        # has a single event loop. anydoc releases the GIL while it runs.
        title, markdown = await asyncio.to_thread(
            # No pypdf fallback here. CRAWL_PDF_FALLBACK exists so that PDFs which
            # used to be readable stay readable; this endpoint is new, has no such
            # history, and is better off with one predictable converter than with a
            # silent second-best whose output a caller cannot distinguish.
            document_to_markdown,
            data,
            resolved,
            pdf_fallback=False,
        )
    except anydoc.ConvertError as exc:
        # A file this parser cannot read is the client's problem, not the
        # server's: 400 with the error class named, so "password-protected" and
        # "truncated" do not arrive as the same opaque failure.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not convert the document — {convert_error_reason(exc)}",
        ) from exc

    return ConvertResponse(
        markdown=markdown,
        format=resolved,
        title=title,
        word_count=len(markdown.split()),
    )


def _named_format(name: str | None) -> anydoc.Format | None:
    """The format the caller named, refusing one this build cannot read."""
    if name is None or not name.strip():
        return None
    fmt = anydoc.format_from_extension(name.strip().lower())
    if fmt is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported format {name!r}. Supported: {', '.join(SUPPORTED_FORMATS)}.",
        )
    return fmt


def _format_from_filename(filename: str | None) -> anydoc.Format | None:
    return anydoc.format_from_path(filename) if filename else None


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read the upload in chunks, stopping the moment it goes over the cap."""
    buffer = bytearray()
    while chunk := await file.read(_READ_CHUNK):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"Upload exceeds the {max_bytes / 1e6:.0f} MB limit.",
            )
    return bytes(buffer)
