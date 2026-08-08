"""Request/response schemas for the crawl endpoint."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, Field

ContentFilter = Literal["pruning", "none"]
CrawlFormat = Literal["markdown", "html", "screenshot", "pdf", "links", "tables"]


def _default_formats() -> list[CrawlFormat]:
    """Typed default for `formats`; a bare lambda infers `list[str]`, not `list[CrawlFormat]`."""
    return ["markdown"]


class CrawlRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "urls": [
                        "https://example.com/blog/scaling-postgres",
                        "https://example.com/blog/incident-2024-11",
                    ],
                    "formats": ["markdown", "links"],
                    "content_filter": "pruning",
                    "prune_threshold": 0.48,
                    "ignore_links": True,
                    "exclude_external_links": False,
                    "word_count_threshold": 20,
                    "include_raw_markdown": False,
                    "cache": True,
                }
            ]
        }
    }

    urls: list[AnyHttpUrl] = Field(..., min_length=1, max_length=50)
    formats: list[CrawlFormat] = Field(
        default_factory=_default_formats,
        description=(
            "Which artefacts to return per page. Only the requested ones are computed "
            "(a screenshot or a PDF render costs real browser time) and only their "
            "fields are populated; everything else stays null."
        ),
    )
    content_filter: ContentFilter = Field(
        default="pruning",
        description="'pruning' returns denser, cleaner markdown; 'none' keeps raw markdown.",
    )
    prune_threshold: float = Field(
        default=0.48, ge=0.0, le=1.0, description="Higher prunes more aggressively."
    )
    ignore_links: bool = Field(default=True, description="Strip links from the markdown output.")
    exclude_external_links: bool = Field(default=False)
    word_count_threshold: int = Field(
        default=0, ge=0, description="Drop text blocks below this many words."
    )
    include_raw_markdown: bool = Field(
        default=False, description="Also return the unfiltered raw markdown."
    )
    cache: bool = Field(default=True, description="Use crawl4ai's local cache when available.")


class CrawledPage(BaseModel):
    url: str
    success: bool
    status_code: int | None = None
    title: str | None = None
    markdown: str = ""
    raw_markdown: str | None = None
    word_count: int = 0
    internal_links: int = 0
    external_links: int = 0
    error: str | None = None
    source_format: str | None = Field(
        default=None,
        description=(
            "What the content was read from: 'html' for a browser-rendered page, or "
            "the document format ('pdf', 'docx', 'xlsx', ...) when the URL served a "
            "document. Lets a caller weigh how much structure to trust."
        ),
    )
    # --- Optional formats (§5.3). Populated only when asked for in `formats`. ---
    html: str | None = Field(
        default=None, description="Cleaned HTML of the page (falls back to the raw HTML)."
    )
    screenshot: str | None = Field(default=None, description="Base64-encoded PNG screenshot.")
    pdf: str | None = Field(
        default=None,
        description="Base64-encoded PDF render of the page. JSON cannot carry raw bytes.",
    )
    links: dict[str, Any] | None = Field(
        default=None, description="Full link map ({'internal': [...], 'external': [...]})."
    )
    tables: list[Any] | None = Field(default=None, description="Tables extracted from the page.")


class CrawlResponse(BaseModel):
    count: int
    results: list[CrawledPage]


class SiteCrawlRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "url": "https://example.com/docs/",
                    "max_depth": 2,
                    "max_pages": 50,
                    "include_external": False,
                    "url_patterns": ["*/docs/*"],
                    "content_filter": "pruning",
                }
            ]
        }
    }

    url: AnyHttpUrl = Field(..., description="Entry point to crawl from.")
    max_depth: int = Field(default=2, ge=0, le=5, description="Link-following depth.")
    max_pages: int = Field(default=25, ge=1, le=500, description="Hard cap on pages crawled.")
    include_external: bool = Field(default=False, description="Follow links to other domains.")
    url_patterns: list[str] | None = Field(
        default=None, description="Only crawl URLs matching these glob patterns."
    )
    content_filter: ContentFilter = Field(default="pruning")


class SiteCrawlResponse(BaseModel):
    base_url: str
    count: int
    pages: list[CrawledPage]
