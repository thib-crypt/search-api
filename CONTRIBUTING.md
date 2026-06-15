# Contributing

Thanks for your interest in improving Unified Search API!

## Development setup

```bash
uv sync --extra dev
cp .env.example .env   # add at least one provider key to try the LLM endpoints
```

Run a local SearXNG (the rest of the API can run on the host):

```bash
docker compose up -d searxng
SEARXNG_URL=http://localhost:8080 uv run uvicorn app.main:app --reload
```

## Checks

Everything must pass before opening a PR:

```bash
uv run ruff check .
uv run pytest -q
```

The default test suite is hermetic — SearXNG, the crawler, and the LLM are all
mocked, so no network or browser is needed. Tests that require real network or a
browser are marked `@pytest.mark.integration` and skipped by default; run them
with `uv run pytest -m integration`.

## Project layout

```
app/
  api/        FastAPI routes, dependencies, middleware
  core/       search (SearXNG), crawl (crawl4ai), llm (litellm), research engine
  jobs/       async job manager + SSE pub/sub
  models/     Pydantic request/response schemas
  services/   orchestration (e.g. /v1/answer)
```

## Conventions

- Async everywhere; share long-lived clients via `app.state`.
- Keep provider-specific details behind `core/llm` (litellm strings).
- Add tests for new endpoints; prefer dependency overrides + fakes over network.
