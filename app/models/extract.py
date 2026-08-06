"""Request/response schemas for the structured-extraction endpoint (§5.1)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

ExtractMode = Literal["css", "llm"]


class ExtractRequest(BaseModel):
    # `schema` shadows a BaseModel attribute, so the field is named `json_schema`
    # internally and exposed under its documented name via an alias.
    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "urls": ["https://example.com/products/mechanical-keyboard"],
                    "schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "price_usd": {"type": "number"},
                            "in_stock": {"type": "boolean"},
                        },
                        "required": ["name", "price_usd"],
                    },
                    "mode": "llm",
                    "model": "openai/gpt-4o-mini",
                }
            ]
        },
    )

    # Capped lower than /v1/crawl: in "llm" mode every URL costs a model call.
    urls: list[AnyHttpUrl] = Field(..., min_length=1, max_length=25)
    json_schema: dict[str, Any] = Field(
        ...,
        alias="schema",
        description=(
            "In 'llm' mode: a JSON Schema object describing the fields to extract. "
            "In 'css' mode: a crawl4ai CSS schema "
            "({'name', 'baseSelector', 'fields': [{'name', 'selector', 'type'}]})."
        ),
    )
    mode: ExtractMode = Field(
        default="llm",
        description="'llm' extracts with a model (generic); 'css' is deterministic and free.",
    )
    model: str | None = Field(
        default=None, description="Override the LLM used in 'llm' mode (litellm string)."
    )


class ExtractResult(BaseModel):
    url: str
    data: dict[str, Any] | None = None
    success: bool = False
    error: str | None = None


class ExtractResponse(BaseModel):
    count: int
    results: list[ExtractResult]
