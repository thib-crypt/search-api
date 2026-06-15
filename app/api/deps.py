"""Shared FastAPI dependencies: optional API-key auth and client accessors."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.search.searxng import SearxngClient
from app.jobs.manager import JobManager


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Enforce API-key auth when AUTH_ENABLED is true; otherwise a no-op."""
    if not settings.auth_enabled:
        return

    provided = x_api_key
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()

    if not provided or provided not in settings.api_key_set:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_searxng(request: Request) -> SearxngClient:
    client: SearxngClient | None = getattr(request.app.state, "searxng", None)
    if client is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="Search backend not initialized.")
    return client


def get_crawler(request: Request) -> CrawlerService:
    crawler: CrawlerService | None = getattr(request.app.state, "crawler", None)
    if crawler is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="Crawler not initialized.")
    return crawler


def get_llm(request: Request) -> LLMClient:
    llm: LLMClient | None = getattr(request.app.state, "llm", None)
    if llm is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="LLM client not initialized.")
    return llm


def get_jobs(request: Request) -> JobManager:
    jobs: JobManager | None = getattr(request.app.state, "jobs", None)
    if jobs is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=503, detail="Job manager not initialized.")
    return jobs
