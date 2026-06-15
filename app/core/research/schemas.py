"""Pydantic schemas for the structured LLM outputs of the research engine.

These are validated by `instructor`, so the field descriptions double as
instructions to the model.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SerpQuery(BaseModel):
    query: str = Field(..., description="The search-engine query string.")
    research_goal: str = Field(
        ...,
        description=(
            "What this query aims to uncover, plus additional research directions "
            "to explore next, in 1-3 sentences."
        ),
    )


class SerpQueryList(BaseModel):
    queries: list[SerpQuery] = Field(default_factory=list)


class SerpProcessing(BaseModel):
    learnings: list[str] = Field(
        default_factory=list,
        description=(
            "Concise, information-dense findings. Prefer entities (people, places, "
            "companies) and exact metrics, numbers, or dates."
        ),
    )
    follow_up_questions: list[str] = Field(
        default_factory=list,
        description="Questions that would deepen the research further.",
    )


class ReportDraft(BaseModel):
    report_markdown: str = Field(..., description="The full final report in Markdown.")


class FeedbackQuestions(BaseModel):
    questions: list[str] = Field(
        default_factory=list,
        description=(
            "Clarifying questions to ask the user before researching. Return an EMPTY "
            "list if the query is already specific and unambiguous."
        ),
    )
