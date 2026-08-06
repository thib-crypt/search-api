"""Request/response schemas for the answer endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.usage import Usage


class AnswerRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": "HNSW vs IVFFlat indexes in pgvector: what are the trade-offs?",
                    "max_sources": 5,
                    "model": "openai/gpt-4o-mini",
                    "language": "en",
                    "categories": ["general"],
                    "fetch_full": True,
                    "stream": False,
                }
            ]
        }
    }

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
    stream: bool = Field(
        default=False,
        description="Stream the answer as Server-Sent Events instead of blocking.",
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
    # §6.5. All zeros on a cache hit: replaying a stored answer spends nothing.
    usage: Usage = Field(default_factory=Usage)
