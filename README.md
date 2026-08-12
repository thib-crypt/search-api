# Unified Search API

A self-hosted, open-source API that unifies three capabilities behind one HTTP surface:

- 🔎 **Web search** — powered by a bundled [SearXNG](https://github.com/searxng/searxng) instance (no third-party search SaaS).
- 🕷️ **Crawling** — single pages or whole sites via [crawl4ai](https://github.com/unclecode/crawl4ai), returning clean Markdown.
- 📄 **Document ingestion** — Word, PowerPoint, Excel, OpenDocument, RTF, EPUB, CSV and PDF read into Markdown via [anydoc](https://github.com/firecrawl/anydoc), **with their tables preserved**.
- 🧠 **Deep research** — iterative, multi-step research reports (a Python port of the [deep-research](https://github.com/dzhng/deep-research) algorithm), driven by the LLM of your choice.
- 🤖 **MCP server** — the same capabilities exposed as [Model Context Protocol](https://modelcontextprotocol.io) tools, served from the same process at `/mcp`.

Everything runs in-process — this is **not** a thin router over hosted services. Bring your own LLM key (OpenAI, Anthropic, Gemini, OpenRouter, …) via [litellm](https://github.com/BerriAI/litellm).

> Status: **early development** — built phase by phase toward a complete v1. See the roadmap below.

## Architecture

```
FastAPI  ──┬── /v1/search       → SearXNG
           ├── /v1/crawl         → crawl4ai (one or many URLs → Markdown)
           ├── /v1/crawl/site    → crawl4ai deep crawl (async job)
           ├── /v1/extract       → crawl + CSS selectors or LLM → structured JSON
           ├── /v1/map           → sitemap.xml / robots.txt / page links → URL list
           ├── /v1/convert       → upload a document (docx, xlsx, pdf, …) → Markdown
           ├── /v1/answer        → search + crawl + 1 LLM pass (fast, sourced)
           ├── /v1/research      → deep-research engine (sync or async job)
           ├── /v1/jobs/{id}     → job status + /stream (Server-Sent Events)
           └── /mcp              → the same capabilities as MCP tools (streamable HTTP)
```

## Quickstart (Docker)

```bash
cp .env.example .env
# edit .env — required before the API will start:
#   API_KEYS        at least one key, as "label:key" (generate: openssl rand -hex 32)
#   SEARXNG_SECRET  openssl rand -hex 32
#   one provider key, e.g. OPENAI_API_KEY
docker compose up --build
curl -H "X-API-Key: <your key>" -H "Content-Type: application/json" \
  http://localhost:8000/v1/search -d '{"q":"hello"}'
curl http://localhost:8000/health   # liveness, never authenticated
```

Auth is **on by default**: `/v1/research` spends real money on your provider keys, so
the app refuses to start with `AUTH_ENABLED=true` and an empty `API_KEYS`. For a
throwaway local run, set `AUTH_ENABLED=false` (a warning is logged unless
`ENVIRONMENT=development`).

Give each client its own labelled key — `API_KEYS=app-mobile:sk_...,test:sk_...`. The
label, never the key, is what the rate limiter, the cost counters and the logs record,
so you can see who spends what, budget them separately, and revoke one key without
touching the others. A bare `key` still works and gets a stable derived label.

## Local development

```bash
uv sync --extra dev
# point the API at a local SearXNG (or run only SearXNG via docker compose up searxng)
SEARXNG_URL=http://localhost:8080 uv run uvicorn app.main:app --reload
```

Checks, in the order CI runs them (cheapest first):

```bash
uv run ruff check app tests          # lint
uv run ruff format --check app tests # formatting
uv run mypy                          # strict, scoped to app/ by pyproject.toml
uv run pytest -q --cov=app           # tests + the 80% coverage gate
```

### Tooling

| Command | What it does |
|---|---|
| `uv run python scripts/bench.py` | In-process latency benchmark (p50/p95) with dependencies faked — measures the API's own overhead. Runs in CI, informational only. |
| `uv sync --extra loadtest && uv run locust -f loadtest/locustfile.py --host ...` | End-to-end load test against a real deployment. See [`loadtest/README.md`](loadtest/README.md). |
| `uv run python scripts/export-openapi.py` | Write `openapi.json` from the app, no server needed. |
| `./scripts/generate-client.sh [typescript-fetch\|python\|...]` | Generate a typed client into `clients/<lang>/` (gitignored). Needs Node (`npx`) and a JRE 11+. |

## Endpoints

| Method & path | Description |
|---|---|
| `POST /v1/search` | Web search via SearXNG (normalized results) |
| `GET /v1/search` | Same search as a cacheable GET (`Cache-Control` + `ETag`) |
| `POST /v1/crawl` | Crawl one or many URLs → clean Markdown |
| `POST /v1/crawl/site` | Deep-crawl a whole site (async job) |
| `POST /v1/extract` | Structured extraction from URLs (CSS selectors or LLM) |
| `POST /v1/map` | Discover a site's URLs (sitemap.xml, robots.txt, page links) |
| `POST /v1/answer` | Fast, source-grounded answer (search + crawl + 1 LLM pass) |
| `POST /v1/research` | Deep-research report (sync, or `background: true` job) |
| `GET /v1/jobs/{id}` · `/stream` | Job status & SSE progress stream |
| `DELETE /v1/jobs/{id}` | Cancel a running job |
| `GET /health` · `/health/ready` · `/` | Liveness, readiness & API metadata |
| `GET /metrics` | Prometheus exposition (requires an API key) |

Interactive API docs are served at `/docs`.

## MCP server

The same capabilities are exposed to AI agents as [Model Context Protocol](https://modelcontextprotocol.io)
tools, served at `MCP_PATH` (default `/mcp`) over the streamable-HTTP transport. Nine
tools, grouped by what they cost — because that is the choice an agent actually has
to make:

| Cost | Tools |
|---|---|
| Free and instant | `web_search`, `map_site`, `deep_research_estimate`, `job_status` |
| Browser time, no LLM | `fetch_url`, `crawl_site` |
| LLM credits | `answer`, `extract_structured` |
| Minutes and dollars | `deep_research` |

It runs **in the API process**, not as a sidecar that would call the HTTP API back over
the network. That is the whole point: one browser pool bounded by `CRAWL_MAX_CONCURRENCY`,
one SearXNG cache, one cost ledger enforcing one daily cap, one job manager. A site crawl
or a research run started by an agent *is* an HTTP job — same id, inspectable at
`GET /v1/jobs/{id}`, streamable at `/stream`, cancellable with `DELETE /v1/jobs/{id}`.
The two surfaces share one bill and one set of brakes rather than racing each other for
the same browser.

Clients authenticate with the same `API_KEYS`, sent as `Authorization: Bearer <key>` —
the same labels, the same constant-time comparison, so an agent's spend lands in the same
per-label counters as everything else. With `AUTH_ENABLED=false` the MCP surface is open,
exactly like `/v1/*`.

```jsonc
// Point an MCP client at a running instance
{
  "mcpServers": {
    "search-api": {
      "type": "http",
      "url": "https://your-host/mcp",
      "headers": { "Authorization": "Bearer <your key>" }
    }
  }
}
```

Two things shape what an agent gets back. Every tool result is capped at
`MCP_MAX_RESULT_TOKENS` (default 4000) — a tool result is not read once and dropped, it
sits in the transcript and is re-sent on every following turn — with the cut marked in
place and the full payload left readable as a `search-api://artifact/{id}` resource. And
`crawl_site` and `deep_research` return a job id immediately instead of blocking for
minutes; the agent polls `job_status`.

For desktop clients that speak only stdio, `python -m app.mcp` runs the same server over a
pipe. Treat it as a **workstation** mode, not a server mode: that process starts its own
browser pool, so `CRAWL_MAX_CONCURRENCY` bounds *that* process and not the fleet.

The full tool catalogue, the resources (`search-api://artifact/…`, `search-api://job/…`,
`search-api://capabilities`), the `research_brief` and `verify_claim` prompts and the
per-client setup live in [`MCP_REFERENCE.md`](MCP_REFERENCE.md).

## Security & hardening

- **API-key auth, on by default** — send `Authorization: Bearer <key>` or `X-API-Key`.
  Keys are compared in constant time and never logged. The app refuses to start
  with auth enabled and no key configured.
- **SSRF guard on every crawled URL** — hostnames are resolved before the browser
  connects, and loopback / private / link-local / CGNAT addresses are refused, as
  are non-`http(s)` schemes and ports outside `CRAWL_ALLOWED_PORTS`. The check runs
  again on the URL a crawl landed on, so a redirect cannot reach the internal
  network. Optional `CRAWL_DOMAIN_ALLOWLIST` / `CRAWL_DOMAIN_DENYLIST`.
- **Rate limiting** weighted by what each endpoint costs (a `/v1/research` call spends
  100 points, a `/v1/search` 1), on by default (`RATE_LIMIT_RPM`), keyed per API-key
  label — or per client IP when auth is off — with a bounded client table.
- **Cost brakes** — `RESEARCH_MAX_COST_USD` stops a runaway run and writes its report
  anyway; `DAILY_COST_CAP_USD` turns a key away for the rest of the UTC day while the
  others keep working; `estimate_only: true` prices a run without launching it. Every
  `/v1/answer` and `/v1/research` response reports its own token and dollar usage.
- **Bounded concurrency** — `CRAWL_MAX_CONCURRENCY` caps browser sessions
  process-wide, not per request; a research run holds one semaphore for the whole
  run rather than one per recursion level.
- **Bounded memory** — job progress history, SSE subscriber queues and finished
  jobs are all capped and swept on a timer.
- **Durable jobs** — with `JOB_BACKEND=sqlite` (the default) jobs, results and progress
  events live on the `./data` volume, so a restart no longer turns a ten-minute report
  into a 404. A job left running by a killed process is reconciled at startup.
- **Security headers** + per-request `X-Request-ID` on every response.
- **Observability** — structured JSON logs (`LOG_FORMAT=json`) where the request id is
  stamped on every record emitted during a request, one access line per request with its
  duration, and an auth-gated `GET /metrics` exposing latency per route template, cache hit
  ratios, crawl outcomes, active jobs and browser sessions, and LLM tokens/cost per key label.
  Optional OpenTelemetry tracing via `OTEL_ENABLED`.
- **CORS** origins configurable via `CORS_ORIGINS`.
- **Container** — base images pinned by digest, dependencies installed from
  `uv.lock`, runtime as a non-root user, SearXNG reachable only on the internal
  compose network.
- In-flight jobs are cancelled cleanly on shutdown.

## Document ingestion

A URL that serves a document is downloaded and converted rather than handed to
the browser, which would only download it again and render an empty page. The
format is decided by the **bytes**, not the file extension or the `Content-Type`
header — document portals mislabel downloads constantly, and plenty serve
`/download?id=4021` with no extension at all.

| | Formats |
|---|---|
| Word | `.doc`, `.docx`, `.docm` |
| Spreadsheets | `.xlsx`, `.xlsm`, `.ods` |
| Presentations | `.ppt`, `.pptx`, `.pptm`, `.ppsx` |
| OpenDocument | `.odt`, `.ods`, `.odp` |
| Other | `.pdf`, `.rtf`, `.epub`, `.csv` |

Tables come back as Markdown tables, headings as headings, lists with their
original numbering. This matters most where it is least visible: text extraction
flattens a table into a column of values, so a model reading it has to *guess*
which number belonged to which row — and it usually guesses right, which is worse
than an obvious error.

This applies wherever crawled content is used, so `/v1/extract`, `/v1/answer` and
`/v1/research` all read documents with no change on their side. Each result
carries a `source_format` field (`"html"`, `"pdf"`, `"xlsx"`, …) saying what it
was read from. `POST /v1/convert` exposes the same conversion for a file you
upload directly.

**Known limitation: no OCR.** A scanned PDF — an image with no text layer — has
nothing to extract and is reported as a failure, not silently returned empty.
Fixing that would mean an OCR model inside the container, which this project
deliberately does not do.

## Deployment

[`docker-compose.coolify.yml`](docker-compose.coolify.yml) deploys the API and its SearXNG
on a [Coolify](https://coolify.io) server as a Docker Compose build pack — no host port
(the proxy routes the domain), no `.env` file (values come from the Coolify UI via `${VAR}`
interpolation), a named volume so SQLite can actually write as the container's non-root
user, and SearXNG's `settings.yml` baked into a small image rather than bind-mounted.
Step by step in [`DEPLOY_COOLIFY.md`](DEPLOY_COOLIFY.md).

Before you start:

- **A 4 GB / 2 vCPU server minimum**, 10 GB of disk. `CRAWL_MAX_CONCURRENCY=5` means up to
  five Chromium instances at once, and the crawl4ai base image is ~2–3 GB.
- **The first build takes 10–20 minutes** (base image + `playwright install chromium`).
  Raise the build timeout if your platform cuts it short.
- **Four required variables** — `API_KEYS`, `LLM_MODEL`, `SEARXNG_SECRET`, and the one
  provider key matching the model prefix (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY` or `GEMINI_API_KEY`). Generate secrets with `openssl rand -hex 32`.
- **Give SearXNG no public domain.** It is unauthenticated and only the API talks to it.

The same file works on any Docker Compose host with a reverse proxy in front; only the
domain and environment-variable plumbing is Coolify-specific.

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
- [x] **Phase 8** — Hot-path performance: ASGI middlewares, GZip + ORJSON, non-blocking tiktoken, search single-flight, LLM retry/timeout/fallback
- [x] **Phase 9** — Research quality & cost: URL/content dedup, pre-crawl rerank, inline citations, SERP filters, report streaming
- [x] **Phase 10** — New endpoints: `/v1/extract`, `/v1/map`, `GET /v1/search`, `/health/ready`, PDF ingestion, answer cache
- [x] **Phase 11** — Persistence & bill protection: SQLite job store, labelled API keys, weighted rate limiting, token/cost accounting, cost circuit breakers, signed webhooks
- [x] **Phase 12** — Observability: JSON logs with request-id correlation and an access line, Prometheus `/metrics` (auth-gated), optional OpenTelemetry tracing
- [x] **Phase 13** — DX & industrialisation: mypy `--strict` and a coverage gate in CI, OpenAPI request examples, OpenAPI schema export + client generation, load tests and an in-process benchmark, Dependabot
- [x] **Phase 14** — MCP server: nine cost-tiered tools mounted in the API process at `/mcp` (streamable HTTP + stdio), sharing the browser pool, cost ledger and jobs with `/v1/*`, per-tool rate-limit weights, capped tool results with the remainder served as artifact resources
- [x] **Phase 15** — Document ingestion: twelve office formats read into Markdown with their tables intact, format detected from the bytes, `POST /v1/convert`

## License

MIT — see [LICENSE](LICENSE).
