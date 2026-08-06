"""Request/response schemas for the URL-discovery endpoint (§5.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field

MapSource = Literal["sitemap", "robots", "page"]


class MapRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "url": "https://example.com/blog/hello-world",
                    "search": "/docs/",
                    "limit": 200,
                }
            ]
        }
    }

    url: AnyHttpUrl = Field(..., description="Any URL on the site; its origin is the entry point.")
    search: str | None = Field(
        default=None, description="Keep only URLs containing this substring (case-insensitive)."
    )
    limit: int = Field(default=100, ge=1, le=1000, description="Max URLs returned.")


class MapResponse(BaseModel):
    links: list[str]
    count: int
    source: MapSource = Field(
        ..., description="Where the URLs came from: sitemap.xml, robots.txt or the page itself."
    )
