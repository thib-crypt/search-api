"""Request/response schemas for the answer endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AnswerRequest(BaseModel):
    query: str = Field(..., min_length=1)
    max_sources: int = Field(default=5, ge=1, le=15, description="How many sources to consult.")
    model: str | None = Field(
        default=None, description="Override the LLM (litellm string, e.g. 'anthropic/claude-...')."
    )
    language: str | None = Field(default=None, description="Search language hint (e.g. 'fr').")
    categories: list[str] | None = Field(default=None)
    fetch_full: bool = Field(
        default=True,
        description="Crawl full page content. If false, answer from search snippets only.",
    )


class Source(BaseModel):
    index: int
    title: str
    url: str
    snippet: str = ""
    used_full_text: bool = False


class AnswerResponse(BaseModel):
    query: str
    answer: str
    model: str
    sources: list[Source]
