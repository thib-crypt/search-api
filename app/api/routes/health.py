"""Health and readiness endpoints.

`/health` is the **liveness** probe: it answers as long as the process is up, and
deliberately depends on nothing else — a restart loop caused by an unreachable
SearXNG would be strictly worse than a degraded API.

`/health/ready` (§5.9) is the **readiness** probe: it says whether this instance
can actually serve traffic right now. Left unauthenticated like `/health`, so an
orchestrator can poll it without holding an API key; it exposes no data beyond
which dependency is unhappy.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import Settings, get_settings

router = APIRouter(tags=["meta"])

# Readiness must be fast: a slow dependency is an unready one.
_PROBE_TIMEOUT = 2.0

# litellm reads provider keys straight from the environment, so readiness checks
# the environment. Pragmatic, not exhaustive: an empty list means the provider
# needs no key (local models).
_PROVIDER_ENV: dict[str, list[str]] = {
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    "vertex_ai": ["GOOGLE_APPLICATION_CREDENTIALS", "VERTEXAI_PROJECT"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "azure": ["AZURE_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "cohere": ["COHERE_API_KEY"],
    "xai": ["XAI_API_KEY"],
    "ollama": [],
}


def _ok(detail: str) -> dict[str, str]:
    return {"status": "ok", "detail": detail}


def _fail(detail: str) -> dict[str, str]:
    return {"status": "error", "detail": detail}


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


async def _check_searxng(request: Request) -> dict[str, str]:
    client = getattr(request.app.state, "searxng", None)
    if client is None:
        return _fail("search backend not initialized")
    try:
        await client.ping(probe_timeout=_PROBE_TIMEOUT)
    except Exception as exc:
        return _fail(str(exc))
    return _ok("reachable")


def _check_crawler(request: Request) -> dict[str, str]:
    crawler = getattr(request.app.state, "crawler", None)
    if crawler is None:
        return _fail("crawler not initialized")
    # A pool that has not been used yet would start fine, so it counts as ready;
    # starting one here would make every probe pay for a browser launch.
    return _ok("browser pool started" if crawler.started else "browser pool not started yet (lazy)")


def _check_llm(settings: Settings) -> dict[str, str]:
    provider = (settings.llm_model.split("/", 1)[0] or "").lower()
    expected = _PROVIDER_ENV.get(provider)
    if expected == []:
        return _ok(f"provider '{provider}' needs no API key")
    if expected and any(os.environ.get(name) for name in expected):
        return _ok(f"provider '{provider}' key configured")
    if expected:
        return _fail(
            f"no API key for provider '{provider}' (expected one of {', '.join(expected)})"
        )

    # Unknown provider prefix: fall back to "is any provider key configured at all".
    configured = [
        name for names in _PROVIDER_ENV.values() for name in names if os.environ.get(name)
    ]
    if configured:
        return _ok(f"provider '{provider}' unknown, but {configured[0]} is set")
    return _fail(f"no provider API key found in the environment for model '{settings.llm_model}'")


@router.get(
    "/health/ready",
    summary="Readiness probe (SearXNG, browser pool, LLM provider key)",
    responses={503: {"description": "At least one dependency is not ready."}},
)
async def ready(request: Request) -> JSONResponse:
    settings = get_settings()
    checks = {
        "searxng": await _check_searxng(request),
        "crawler": _check_crawler(request),
        "llm": _check_llm(settings),
    }
    failed = [name for name, check in checks.items() if check["status"] != "ok"]
    body: dict[str, Any] = {
        "status": "not_ready" if failed else "ready",
        "version": __version__,
        "checks": checks,
    }
    if failed:
        body["failed"] = failed
        return JSONResponse(status_code=503, content=body)
    return JSONResponse(status_code=200, content=body)
