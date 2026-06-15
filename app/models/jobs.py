"""Schemas for asynchronous job submission and inspection."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class JobSubmitted(BaseModel):
    job_id: str
    type: str
    status: str
    stream_url: str


class JobView(BaseModel):
    id: str
    type: str
    status: str
    created_at: str
    updated_at: str
    progress: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    error: str | None = None
