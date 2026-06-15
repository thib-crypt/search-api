"""Request/response schemas for the deep-research endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Tri-state clarification control:
#   "auto"  (default) -> the model decides whether to ask clarifying questions
#   true              -> always run the clarification step
#   false             -> never ask, research immediately
Interactive = bool | Literal["auto"]


class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    breadth: int | None = Field(
        default=None, ge=1, le=10, description="SERP queries per level (default from config)."
    )
    depth: int | None = Field(
        default=None, ge=1, le=5, description="Recursion levels (default from config)."
    )
    model: str | None = Field(default=None, description="Override LLM (litellm string).")
    interactive: Interactive = Field(default="auto")
    answers: list[str] | None = Field(
        default=None,
        description="Answers to previously returned clarifying questions; triggers the run.",
    )
    background: bool = Field(
        default=False,
        description="Run as an async job and return a job id instead of blocking.",
    )


class ResearchResponse(BaseModel):
    status: Literal["completed", "needs_clarification"]
    query: str
    model: str
    # Present when status == "needs_clarification".
    questions: list[str] = []
    # Present when status == "completed".
    report: str | None = None
    learnings: list[str] = []
    sources: list[str] = []
    serp_queries: list[str] = []
