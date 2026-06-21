# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (includes dev extras: pytest, ruff, mypy, respx)
uv sync --extra dev

# Run the API locally (requires a SearXNG instance)
docker compose up -d searxng
SEARXNG_URL=http://localhost:8080 uv run uvicorn app.main:app --reload

# Run with Docker (full stack)
cp .env.example .env   # set at least one LLM key + SEARXNG_SECRET
docker compose up --build

# Lint
uv run ruff check .

# Run all tests (hermetic — no network or browser required)
uv run pytest -q

# Run a single test file
uv run pytest tests/test_search.py -q

# Run integration tests (real network + browser)
uv run pytest -m integration
```

## Architecture

The app is a FastAPI service with three capabilities — web search, crawling, and deep research — composed by `app/main.py`.

**Shared clients live on `app.state`** and are constructed once in the `lifespan` context manager. Routes access them via FastAPI dependency functions in `app/api/deps.py` (`get_searxng`, `get_crawler`, `get_llm`, `get_jobs`). Shutdown in `lifespan` cancels in-flight jobs before tearing down clients.

**Layers:**

| Layer | Path | Responsibility |
|---|---|---|
| Routes | `app/api/routes/` | HTTP input/output, wires services/engine to requests |
| Services | `app/services/` | Orchestration logic (e.g. `/v1/answer`: search + crawl + 1 LLM call) |
| Core | `app/core/` | Stateful clients: `SearxngClient`, `CrawlerService`, `LLMClient`, `DeepResearchEngine` |
| Jobs | `app/jobs/manager.py` | In-process async job store + SSE pub/sub |
| Models | `app/models/` | Pydantic request/response schemas |
| Config | `app/config.py` | Pydantic-settings; all settings come from env vars (see `.env.example`) |

**LLM client (`app/core/llm/client.py`):** Wraps `litellm` + `instructor`. `complete()` returns plain text; `structured()` returns a validated Pydantic model (via instructor). All LLM model identifiers use litellm strings (`openai/gpt-4o-mini`, `anthropic/claude-...`). `litellm.drop_params = True` silently drops unsupported params across providers. A separate `LLM_REPORT_MODEL` can target a stronger model only for final report writing.

**Deep research engine (`app/core/research/engine.py`):** Recursive async algorithm: generate SERP queries → search + crawl → extract learnings + follow-up questions (structured LLM) → recurse with `breadth/2` and `depth-1`. At depth 0 it writes the final Markdown report. Concurrency inside each level is controlled by a semaphore. The engine takes an optional `on_progress` hook consumed by SSE when running as a background job. Tuning constants (`RESULTS_PER_QUERY`, `PER_PAGE_TOKEN_BUDGET`, etc.) are module-level in the engine file.

**Research clarification flow:** `/v1/research` has a tri-state `interactive` field (`"auto"` / `true` / `false`). When `"auto"`, the LLM decides whether to return clarifying questions (status `"needs_clarification"`). The client re-submits with the original query + `answers`. Background execution via `background: true` returns a `job_id` and `stream_url` immediately.

**Job system (`app/jobs/manager.py`):** In-memory only (abstracted surface ready for Redis). `JobManager.submit()` wraps a coroutine in an `asyncio.Task`. Progress events are appended to `job.progress` and fanned out to all SSE subscriber queues. `stream()` replays history first, then delivers live events. Terminal jobs are evicted after `JOB_RETENTION_SECONDS`.

**Search cache (`app/core/cache.py`):** A generic `TTLCache[V]` (async-safe LRU + TTL using `OrderedDict` + `asyncio.Lock`). Used by `SearxngClient` to deduplicate identical queries — critical for the research engine which fans out overlapping SERP queries.

**Middleware order matters:** Registered in reverse execution order. `RateLimitMiddleware` is added last so it runs first. `/health`, `/docs`, `/openapi.json`, `/redoc` are exempt from rate limiting.

## Conventions

- All I/O is async. Long-lived clients (`SearxngClient`, `CrawlerService`, `LLMClient`) are singletons on `app.state`, never instantiated per-request.
- Settings are read via `get_settings()` (`@lru_cache`). In tests, call `get_settings.cache_clear()` around `monkeypatch.setenv` to avoid stale cached values.
- Tests use dependency overrides (`app.dependency_overrides`) and `respx.mock` for HTTP mocking — no real network. New endpoint tests should follow this pattern.
- `ruff` line length is 100; rules `E`, `F`, `I`, `UP`, `B`, `ASYNC` are active; `B008` (FastAPI `Depends()` in defaults) is ignored.
- `pyproject.toml` uses `asyncio_mode = "auto"` so all async test functions run without `@pytest.mark.asyncio`.
- `@pytest.mark.integration` marks tests requiring real network/browser; they are excluded from default CI runs.
