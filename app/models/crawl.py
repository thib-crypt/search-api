"""Request/response schemas for the crawl endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field

ContentFilter = Literal["pruning", "none"]


class CrawlRequest(BaseModel):
    urls: list[AnyHttpUrl] = Field(..., min_length=1, max_length=50)
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


class CrawlResponse(BaseModel):
    count: int
    results: list[CrawledPage]


class SiteCrawlRequest(BaseModel):
    url: AnyHttpUrl = Field(..., description="Entry point to crawl from.")
    max_depth: int = Field(default=2, ge=0, le=5, description="Link-following depth.")
    max_pages: int = Field(default=25, ge=1, le=500, description="Hard cap on pages crawled.")
    include_external: bool = Field(
        default=False, description="Follow links to other domains."
    )
    url_patterns: list[str] | None = Field(
        default=None, description="Only crawl URLs matching these glob patterns."
    )
    content_filter: ContentFilter = Field(default="pruning")


class SiteCrawlResponse(BaseModel):
    base_url: str
    count: int
    pages: list[CrawledPage]
