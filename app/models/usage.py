"""What a request cost (§6.5).

Shared by `/v1/answer` and `/v1/research` so a client reads the same shape
whichever endpoint it called. The dollar figure comes from litellm's model cost
map and is an **estimate**: a model litellm cannot price contributes tokens but
no cost, so `total_cost_usd` is a floor, not an invoice.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_cost_usd: float = Field(
        default=0.0, description="Estimated spend, from litellm's model cost map."
    )
    searches: int = Field(default=0, description="SearXNG queries issued.")
    pages_crawled: int = Field(default=0, description="Pages successfully fetched and read.")
    pages_skipped_by_rerank: int = Field(
        default=0,
        description="SERP results the pre-crawl reranker dropped — pages this run did not pay for.",
    )

    @classmethod
    def from_tracker(cls, tracker: Any) -> Usage:
        """Build from a `app.core.llm.usage.UsageTracker` snapshot."""
        return cls(**tracker.snapshot())
