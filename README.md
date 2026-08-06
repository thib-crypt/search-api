# Unified Search API

A self-hosted, open-source API that unifies three capabilities behind one HTTP surface:

- 🔎 **Web search** — powered by a bundled [SearXNG](https://github.com/searxng/searxng) instance (no third-party search SaaS).
- 🕷️ **Crawling** — single pages or whole sites via [crawl4ai](https://github.com/unclecode/crawl4ai), returning clean Markdown.
- 🧠 **Deep research** — iterative, multi-step research reports (a Python port of the [deep-research](https://github.com/dzhng/deep-research) algorithm), driven by the LLM of your choice.

Everything runs in-process — this is **not** a thin router over hosted services. Bring your own LLM key (OpenAI, Anthropic, Gemini, OpenRouter, …) via [litellm](https://github.com/BerriAI/litellm).

> Status: **early development** — built phase by phase toward a complete v1. See the roadmap below.

## Architecture

```
FastAPI  ──┬── /v1/search       → SearXNG
           ├── /v1/crawl         → crawl4ai (one or many URLs → Markdown)
           ├── /v1/crawl/site    → crawl4ai deep crawl (async job)
           ├── /v1/answer        → search + crawl + 1 LLM pass (fast, sourced)
           ├── /v1/research      → deep-research engine (sync or async job)
           └── /v1/jobs/{id}     → job status + /stream (Server-Sent Events)
```

## Quickstart (Docker)

```bash
cp .env.example .env
# edit .env — required before the API will start:
#   API_KEYS        at least one key (generate: openssl rand -hex 32)
#   SEARXNG_SECRET  openssl rand -hex 32
#   one provider key, e.g. OPENAI_API_KEY
docker compose up --build
curl -H "X-API-Key: <your key>" http://localhost:8000/v1/search -d '{"q":"hello"}'
curl http://localhost:8000/health   # liveness, never authenticated
```

Auth is **on by default**: `/v1/research` spends real money on your provider keys, so
the app refuses to start with `AUTH_ENABLED=true` and an empty `API_KEYS`. For a
throwaway local run, set `AUTH_ENABLED=false` (a warning is logged unless
`ENVIRONMENT=development`).

## Local development

```bash
uv sync --extra dev
# point the API at a local SearXNG (or run only SearXNG via docker compose up searxng)
SEARXNG_URL=http://localhost:8080 uv run uvicorn app.main:app --reload
```

## Endpoints

| Method & path | Description |
|---|---|
| `POST /v1/search` | Web search via SearXNG (normalized results) |
| `POST /v1/crawl` | Crawl one or many URLs → clean Markdown |
| `POST /v1/crawl/site` | Deep-crawl a whole site (async job) |
| `POST /v1/answer` | Fast, source-grounded answer (search + crawl + 1 LLM pass) |
| `POST /v1/research` | Deep-research report (sync, or `background: true` job) |
| `GET /v1/jobs/{id}` · `/stream` | Job status & SSE progress stream |
| `GET /health` · `/` | Liveness & API metadata |

Interactive API docs are served at `/docs`.

## Security & hardening

- **API-key auth, on by default** — send `Authorization: Bearer <key>` or `X-API-Key`.
  Keys are compared in constant time and never logged. The app refuses to start
  with auth enabled and no key configured.
- **SSRF guard on every crawled URL** — hostnames are resolved before the browser
  connects, and loopback / private / link-local / CGNAT addresses are refused, as
  are non-`http(s)` schemes and ports outside `CRAWL_ALLOWED_PORTS`. The check runs
  again on the URL a crawl landed on, so a redirect cannot reach the internal
  network. Optional `CRAWL_DOMAIN_ALLOWLIST` / `CRAWL_DOMAIN_DENYLIST`.
- **Rate limiting** per client IP, on by default (`RATE_LIMIT_RPM`), with a bounded
  client table.
- **Bounded concurrency** — `CRAWL_MAX_CONCURRENCY` caps browser sessions
  process-wide, not per request; a research run holds one semaphore for the whole
  run rather than one per recursion level.
- **Bounded memory** — job progress history, SSE subscriber queues and finished
  jobs are all capped and swept on a timer.
- **Security headers** + per-request `X-Request-ID` on every response.
- **CORS** origins configurable via `CORS_ORIGINS`.
- **Container** — base images pinned by digest, dependencies installed from
  `uv.lock`, runtime as a non-root user, SearXNG reachable only on the internal
  compose network.
- In-flight jobs are cancelled cleanly on shutdown.

## Configuration

All settings are environment variables — see [`.env.example`](.env.example).

## Roadmap

- [x] **Phase 0** — Project scaffold, config, Docker, SearXNG, healthcheck
- [x] **Phase 1** — `/v1/search` (SearXNG client, normalized results, optional API-key auth)
- [x] **Phase 2** — `/v1/crawl` (single + multi-URL via crawl4ai, clean `fit_markdown`, bounded concurrency)
- [x] **Phase 3** — LLM layer (litellm + instructor, per-request model override) + `/v1/answer` (sourced, cited)
- [x] **Phase 4** — `/v1/research` deep-research engine (recursive breadth/depth, structured learnings, tri-state clarification)
- [x] **Phase 5** — Async jobs + SSE streaming (`/v1/jobs/{id}`, `/stream`), background research, `/v1/crawl/site` (deep crawl)
- [x] **Phase 6** — Hardening: rate limiting, security headers, request IDs, graceful job shutdown, CORS config, docs & Docker polish
- [x] **Phase 7** — Correctness & security: SSRF guard, global concurrency caps, bounded job/SSE/rate-limit memory, constant-time auth on by default, container hardening
- [ ] **Phase 8** — Hot-path performance: ASGI middlewares, GZip + ORJSON, non-blocking tiktoken, search single-flight, LLM retry/timeout/fallback
- [ ] **Later** — see [`IMPROVEMENT_PLAN.md`](IMPROVEMENT_PLAN.md) for phases 9–13

## License

MIT — see [LICENSE](LICENSE).
