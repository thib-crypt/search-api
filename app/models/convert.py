"""Response schema for the document-conversion endpoint (§15.5).

There is no request model: `POST /v1/convert` takes ``multipart/form-data``, so
its inputs are declared as ``File``/``Form`` parameters on the route itself.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConvertResponse(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "markdown": (
                        "# Rapport trimestriel\n\n|Region|T1|T2|\n|---|---|---|\n"
                        "|EMEA|1 200|1 450|\n"
                    ),
                    "format": "xlsx",
                    "title": "Rapport trimestriel",
                    "word_count": 12,
                }
            ]
        }
    }

    markdown: str = Field(description="GitHub-Flavored Markdown rendering of the document.")
    format: str = Field(
        description=(
            "The format the document was actually read as. May differ from the "
            "`format` field of the request when the bytes said otherwise, and from "
            "the file's extension for container variants (a .docm reads as `docx`)."
        )
    )
    title: str | None = Field(
        default=None,
        description="The document's first heading, or null when it opens without one.",
    )
    word_count: int = Field(description="Words in `markdown`.")
