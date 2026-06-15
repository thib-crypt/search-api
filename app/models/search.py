"""Request/response schemas for the search endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TimeRange = Literal["day", "week", "month", "year"]
SafeSearch = Literal[0, 1, 2]


class SearchRequest(BaseModel):
    q: str = Field(..., min_length=1, description="Search query.")
    categories: list[str] | None = Field(
        default=None, description="SearXNG categories, e.g. ['general', 'news']."
    )
    engines: list[str] | None = Field(
        default=None, description="Restrict to specific SearXNG engines."
    )
    language: str | None = Field(default=None, description="Language code, e.g. 'en', 'fr'.")
    pageno: int = Field(default=1, ge=1, description="Result page number.")
    time_range: TimeRange | None = Field(default=None)
    safesearch: SafeSearch | None = Field(default=None)
    max_results: int | None = Field(
        default=None, ge=1, le=100, description="Truncate to at most this many results."
    )


class SearchResultItem(BaseModel):
    title: str
    url: str
    snippet: str = ""
    engine: str | None = None
    score: float | None = None
    category: str | None = None
    published_date: str | None = None


class SearchResponse(BaseModel):
    query: str
    number_of_results: int
    results: list[SearchResultItem]
    suggestions: list[str] = []
    answers: list[str] = []
