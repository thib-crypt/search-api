"""Request/response schemas for the deep-research endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.models.search import TimeRange
from app.models.usage import Usage

# Tri-state clarification control:
#   "auto"  (default) -> the model decides whether to ask clarifying questions
#   true              -> always run the clarification step
#   false             -> never ask, research immediately
Interactive = bool | Literal["auto"]


class ResearchRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": "How are production RAG systems handling document chunking in 2025?",
                    "breadth": 4,
                    "depth": 2,
                    "model": "openai/gpt-4o-mini",
                    "interactive": False,
                    "background": True,
                    "stream": False,
                    "time_range": "year",
                    "exclude_domains": ["reddit.com"],
                    "estimate_only": False,
                }
            ]
        }
    }

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
    stream: bool = Field(
        default=False,
        description=(
            "Stream the final report token by token over the job's SSE channel. "
            "Implies background=true: GET /v1/jobs/{id}/stream is the only SSE channel."
        ),
    )
    # --- SERP filters (§5.6) ---
    time_range: TimeRange | None = Field(
        default=None, description="Restrict SERP results to the last day/week/month/year."
    )
    include_domains: list[str] | None = Field(
        default=None, description="Only research sources on these domains (subdomains included)."
    )
    exclude_domains: list[str] | None = Field(
        default=None, description="Never research sources on these domains (subdomains included)."
    )
    # --- Cost control (§6.4) ---
    estimate_only: bool = Field(
        default=False,
        description=(
            "Return a projected cost and page count without searching, crawling or "
            "calling any model. Answers with status='estimated'."
        ),
    )
    # --- Webhook (§5.8) ---
    webhook_url: str | None = Field(
        default=None,
        description=(
            "POSTed a signed JSON payload when a background research job finishes "
            "(success or failure). Only meaningful with background=true or stream=true. "
            "Goes through the same SSRF guard as any other server-fetched URL, and "
            "requires WEBHOOK_SECRET to be configured."
        ),
    )


class Citation(BaseModel):
    """One entry of the numbered source map the report's [n] markers refer to."""

    index: int = Field(..., ge=1, description="The number used in the report's [n] markers.")
    url: str


class ResearchEstimate(BaseModel):
    """Projected cost of a run that has not been launched (§6.4c).

    Deliberately coarse — see `app.core.research.estimate` for the method. Its
    job is to let a client decide whether to commit, not to invoice.
    """

    serp_queries: int = Field(..., description="SERP queries the run would generate in total.")
    searches: int = Field(..., description="SearXNG queries that implies.")
    pages_crawled: int = Field(..., description="Pages the run would fetch and read.")
    llm_calls: int = Field(..., description="Model round-trips, reranking and report included.")
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = Field(..., description="Projected spend, in USD.")
    model: str = Field(..., description="Model the projection was priced against.")


class ResearchResponse(BaseModel):
    status: Literal["completed", "needs_clarification", "estimated"]
    query: str
    model: str
    # Present when status == "needs_clarification".
    questions: list[str] = []
    # Present when status == "completed".
    report: str | None = None
    learnings: list[str] = []
    sources: list[str] = []
    # Resolves the report's inline [n] markers to URLs, so any claim can be checked.
    citations: list[Citation] = []
    serp_queries: list[str] = []
    # §6.5: what this run actually cost so far. Present on "needs_clarification"
    # too -- asking the clarifying questions is itself a model call, and it is
    # billed to the key like any other. Absent only on "estimated", which spends
    # nothing (see `ResearchEstimate` for the projection that branch returns).
    usage: Usage | None = None
    # §6.4a: the run hit RESEARCH_MAX_COST_USD and wrote its report early.
    stopped_early: bool = False
    # Present when status == "estimated".
    estimate: ResearchEstimate | None = None
