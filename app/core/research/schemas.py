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


class LearningDraft(BaseModel):
    """One finding, tied back to the numbered sources it came from.

    Indices are asked for rather than URLs: they cost a couple of tokens instead
    of fifty, and a model cannot mistype them into a source that does not exist
    (out-of-range values are dropped by the engine).
    """

    text: str = Field(
        ...,
        description=(
            "A concise, information-dense finding. Prefer entities (people, places, "
            "companies) and exact metrics, numbers, or dates."
        ),
    )
    source_indices: list[int] = Field(
        default_factory=list,
        description=(
            "1-based numbers of the sources this finding is taken from, as shown in "
            "the [n] markers of the provided contents."
        ),
    )


class SerpProcessing(BaseModel):
    learnings: list[LearningDraft] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(
        default_factory=list,
        description="Questions that would deepen the research further.",
    )


class Learning(BaseModel):
    """A learning once its source indices have been resolved to URLs.

    Not an LLM output: this is what the engine carries between steps and what
    gives the final report its inline citations.
    """

    text: str
    source_urls: list[str] = Field(default_factory=list)


class RankedResults(BaseModel):
    keep: list[int] = Field(
        default_factory=list,
        description=("0-based indices of the search results worth crawling, most relevant first."),
    )
    dropped_reason: dict[int, str] = Field(
        default_factory=dict,
        description="Index -> short reason, for the results that were rejected.",
    )


class FeedbackQuestions(BaseModel):
    questions: list[str] = Field(
        default_factory=list,
        description=(
            "Clarifying questions to ask the user before researching. Return an EMPTY "
            "list if the query is already specific and unambiguous."
        ),
    )
