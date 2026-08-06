# API Reference

Complete reference for the Unified Search API v0.1.0.

Interactive docs are also served at `/docs` (Swagger UI) and `/redoc` when the server is running.

---

## Table of Contents

- [Base URL & versioning](#base-url--versioning)
- [Authentication](#authentication)
- [Request IDs](#request-ids)
- [Logging](#logging)
- [Rate limiting](#rate-limiting)
- [Cost controls](#cost-controls)
- [Webhooks](#webhooks)
- [Error format](#error-format)
- [Endpoints](#endpoints)
  - [GET /](#get-)
  - [GET /health](#get-health)
  - [GET /health/ready](#get-healthready)
  - [GET /metrics](#get-metrics)
  - [POST /v1/search](#post-v1search)
  - [GET /v1/search](#get-v1search)
  - [POST /v1/crawl](#post-v1crawl)
  - [POST /v1/crawl/site](#post-v1crawlsite)
  - [POST /v1/extract](#post-v1extract)
  - [POST /v1/map](#post-v1map)
  - [POST /v1/answer](#post-v1answer)
  - [POST /v1/research](#post-v1research)
  - [GET /v1/jobs/{job\_id}](#get-v1jobsjob_id)
  - [DELETE /v1/jobs/{job\_id}](#delete-v1jobsjob_id)
  - [GET /v1/jobs/{job\_id}/stream](#get-v1jobsjob_idstream)
- [Schemas](#schemas)
- [Configuration reference](#configuration-reference)
- [SSE streaming format](#sse-streaming-format)

---

## Base URL & versioning

```
http://<host>:8000
```

All resource endpoints are prefixed with `/v1`. Utility endpoints (`/`, `/health`, `/health/ready`,
`/metrics`) have no prefix.

---

## Authentication

Authentication is **enabled by default** (`AUTH_ENABLED=true`). `/v1/research` triggers dozens of
billed LLM calls on your provider keys, so an open instance costs real money — the app **refuses to
start** when `AUTH_ENABLED=true` and `API_KEYS` is empty, rather than accepting every request.

Every `/v1/*` endpoint requires a valid API key. Send it in either header — the first one found is used:

| Header | Format |
|---|---|
| `Authorization` | `Bearer <key>` |
| `X-API-Key` | `<key>` |

Valid keys are set via the `API_KEYS` environment variable (comma-separated list). Generate one with
`openssl rand -hex 32`. Keys are compared in constant time and are never written to the logs.
`/health` and `/` stay open so probes keep working.

### Labeled keys

Each `API_KEYS` entry is either `label:key` or a bare `key`:

```
API_KEYS=app-mobile:sk_a1b2...,script-perso:sk_c3d4...,test:sk_e5f6...
```

A bare key still works (backward compatible) and receives a stable derived label. The **label** — never
the key — is what the server uses for rate-limit buckets, daily cost counters and any log line, so giving
each client its own label means you can see who spends what, budget them separately, and revoke one key
by deleting a single line and restarting. With `AUTH_ENABLED=false` every caller shares the label
`anonymous`.

**401 response when auth fails:**

```json
{
  "detail": "Missing or invalid API key."
}
```

> `/`, `/health`, `/health/ready`, `/docs`, `/openapi.json`, and `/redoc` are always public.
> `/metrics` is **not** — it publishes per-key spend, so it needs an API key like any `/v1/*` route.

---

## Request IDs

Every response includes an `X-Request-ID` header. The server either mirrors the value you send in the request header, or generates a UUID automatically. Use this ID when reporting bugs or correlating logs.

---

## Logging

`LOG_FORMAT=json` (the default) emits **one JSON object per line** on stdout:

```json
{"timestamp":"2026-01-30T10:12:44.318+00:00","level":"INFO","logger":"unified_search_api.access","message":"POST /v1/answer 200 812.4ms","request_id":"6b1c...","method":"POST","path":"/v1/answer","status_code":200,"duration_ms":812.412}
```

- `request_id` is stamped on **every** record emitted while a request is in flight — not just
  the access line — so the log of a failing research run can be filtered down to that one request
  with the `X-Request-ID` the client received.
- One access line is written per request, timed around the whole exchange: an SSE response is
  reported with the duration of its *stream*, not of its headers.
- Exceptions carry their traceback in an `exception` field.

Set `LOG_FORMAT=text` for the plain `LEVEL:logger:message [request_id=...]` one-liner when reading
a local run by eye.

---

## Rate limiting

Enabled by default (`RATE_LIMIT_ENABLED=true`), at `RATE_LIMIT_RPM` **points** per 60-second sliding
window (default `120`).

A request costs points according to what the endpoint spends downstream, not a flat 1 — a `/v1/research`
call is roughly a thousand times a `/v1/search` in provider spend, so a flat counter would let 120
deep-research runs a minute through:

| Path | Weight |
|---|---|
| `/v1/search` | 1 |
| `/v1/map` | 2 |
| `/v1/crawl` | 5 |
| `/v1/answer`, `/v1/extract` | 10 |
| `/v1/crawl/site` | 25 |
| `/v1/research` | 100 |
| anything else (e.g. `/v1/jobs/*`) | 1 |

The window is keyed on the caller's **API-key label** when auth is enabled, and on the client IP
otherwise — so one client exhausting its budget never starves another sharing the same egress IP.
Per-label budgets override the global one via `RATE_LIMIT_RPM_OVERRIDES=app-mobile:500,test:20`.

Every rate-limited response (not only 429s) carries the standard headers:

```
RateLimit-Limit: 120
RateLimit-Remaining: 20
RateLimit-Reset: 47
```

Exceeding the limit returns:

```
HTTP 429 Too Many Requests
Retry-After: <seconds>
```

Exempt paths: `/health`, `/health/ready`, `/docs`, `/openapi.json`, `/redoc`, `/metrics`.

---

## Cost controls

`/v1/research` and `/v1/answer` spend real provider credits, so three independent brakes are available.
All are off by default.

| Control | Setting | Behaviour |
|---|---|---|
| Per-run ceiling | `RESEARCH_MAX_COST_USD` | A run passing the ceiling stops exploring and writes its report from what it already gathered. The response carries `stopped_early: true` and the report says so. Not an error. |
| Per-key daily cap | `DAILY_COST_CAP_USD` | Spend is totalled per API-key label per UTC day, across both endpoints. A label over its cap gets a `429` **before** any work starts, while every other label keeps working. Durable with `JOB_BACKEND=sqlite`; with `JOB_BACKEND=memory` the counters reset on restart. |
| Dry run | `estimate_only: true` on `/v1/research` | Returns a projected cost and page count without searching, crawling or calling any model. |

**429 when the daily cap is reached:**

```json
{
  "detail": "Daily cost cap reached for this API key: $12.4100 spent of $10.00 allowed (UTC day). The budget resets at 00:00 UTC."
}
```

### Usage accounting

`/v1/answer` and `/v1/research` report what the call cost, in a `usage` object:

```json
{
  "usage": {
    "prompt_tokens": 48210,
    "completion_tokens": 3120,
    "total_cost_usd": 0.021,
    "searches": 12,
    "pages_crawled": 27,
    "pages_skipped_by_rerank": 43
  }
}
```

`total_cost_usd` comes from litellm's model cost map and is an **estimate**: a model litellm cannot price
contributes tokens but no cost, so treat the figure as a floor. A cached `/v1/answer` response reports all
zeros — replaying a stored answer spends nothing. On a streaming answer the same object is the payload of
the final `done` event; on a background research job it is the last field of the job's `result`.

---

## Webhooks

A background research run can take ten minutes. Instead of polling `GET /v1/jobs/{id}`, pass a
`webhook_url` alongside `background: true` (or `stream: true`) and the server will POST the outcome when
the job finishes — on **success and on failure**, since a client waiting for a callback needs to hear
about both.

```jsonc
POST /v1/research
{ "query": "...", "background": true, "webhook_url": "https://example.com/hooks/research" }
```

The callback body is JSON and carries an HMAC-SHA256 signature over the exact bytes sent:

```
POST /hooks/research
Content-Type: application/json
X-Webhook-Signature: sha256=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08
```

```jsonc
// success
{ "job_id": "3f2a…", "type": "research", "status": "completed", "result": { /* same shape as the job's result */ } }
// failure
{ "job_id": "3f2a…", "type": "research", "status": "failed", "error": "…", "usage": { /* … */ } }
```

Verify it by recomputing `hmac_sha256(WEBHOOK_SECRET, raw_body)` and comparing in constant time.

Two rules to know about:

- **`WEBHOOK_SECRET` must be set.** A request carrying a `webhook_url` while the secret is empty is
  refused with a `400`: an unsigned callback cannot be authenticated by its receiver. Generate one with
  `openssl rand -hex 32`.
- **The URL goes through the SSRF guard.** It is a client-supplied address the *server* fetches, so it is
  checked exactly like a crawl target — a `webhook_url` pointing at `169.254.169.254`, a private range or
  a non-HTTP scheme is refused with a `400` before the job is created.

A webhook that times out, refuses the connection or answers 5xx is logged and dropped. It never fails the
job: the result is already stored and remains available from `GET /v1/jobs/{id}`.

---

## Error format

All errors follow FastAPI's default structure:

```json
{
  "detail": "<human-readable message>"
}
```

| Status | Meaning |
|---|---|
| `400` | Bad request — e.g. a URL refused by the network guard, or a `webhook_url` without `WEBHOOK_SECRET` |
| `401` | Missing or invalid API key |
| `404` | Resource not found (e.g. unknown job ID) |
| `409` | Job already in a terminal state (`DELETE /v1/jobs/{id}`) |
| `422` | Request body failed validation, or an unusable extraction schema |
| `429` | Rate limit exceeded, or the daily cost cap reached |
| `500` | Unexpected server error |
| `502` | Upstream failure (SearXNG unreachable, crawl error, LLM error) |
| `503` | Service not yet initialised, or `/health/ready` reporting a failing dependency |
| `504` | A synchronous `/v1/research` run exceeded `RESEARCH_SYNC_TIMEOUT` |

Unhandled `500` errors include the `X-Request-ID` value in the response body to help with debugging.

---

## Endpoints

### GET /

Returns API metadata and a list of available endpoints.

**Auth required:** No

**Response `200`:**

```json
{
  "name": "Unified Search API",
  "version": "0.1.0",
  "docs": "/docs",
  "endpoints": ["/v1/search", "/v1/crawl", "/v1/crawl/site", "/v1/extract", "/v1/map", "/v1/answer", "/v1/research", "/v1/jobs/{id}"]
}
```

---

### GET /health

Liveness probe. Returns immediately; does not test upstream services.

**Auth required:** No

**Response `200`:**

```json
{
  "status": "ok",
  "version": "0.1.0"
}
```

---

### GET /health/ready

Readiness probe. Unlike `/health`, it actually tests the things a request needs:

| Check | What it verifies |
|---|---|
| `searxng` | `GET {SEARXNG_URL}/healthz` answers within 2 s and is not a 5xx |
| `crawler` | The crawler service exists. A browser pool that has not started yet counts as ready — it starts lazily on the first crawl, and launching one just to answer a probe would make every probe expensive |
| `llm` | At least one provider API key for `LLM_MODEL`'s provider is present in the environment (litellm reads keys from the environment directly) |

`/health` is unaffected by any of this and stays a pure liveness probe.

**Auth required:** No (so an orchestrator can poll it without a key)

**Response `200`** — every check passed:

```json
{
  "status": "ready",
  "version": "0.1.0",
  "checks": {
    "searxng": { "status": "ok", "detail": "reachable" },
    "crawler": { "status": "ok", "detail": "browser pool not started yet (lazy)" },
    "llm": { "status": "ok", "detail": "provider 'openai' key configured" }
  }
}
```

**Response `503`** — at least one check failed. `failed` lists the failing checks; the
others still report, so one probe shows the whole picture:

```json
{
  "status": "not_ready",
  "version": "0.1.0",
  "failed": ["searxng"],
  "checks": {
    "searxng": { "status": "error", "detail": "Could not reach SearXNG at http://searxng:8080: ..." },
    "crawler": { "status": "ok", "detail": "browser pool started" },
    "llm": { "status": "ok", "detail": "provider 'openai' key configured" }
  }
}
```

---

### GET /metrics

Prometheus exposition, in the standard text format
(`Content-Type: text/plain; version=1.0.0`).

**Auth required:** Yes — the payload includes `llm_cost_usd_total` and `llm_tokens_total`
**labelled per API-key label**, i.e. what each of your clients spends. Scrape it with a dedicated
key, or keep the route on an internal network at the reverse proxy. It is **exempt from rate
limiting**: a scrape runs on its own schedule and must not draw down a caller's budget.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `http_request_duration_seconds` | histogram | `method`, `route`, `status_code` | Request latency. `route` is the *template* (`/v1/jobs/{job_id}`), never the raw path |
| `http_requests_total` | counter | `method`, `route`, `status_code` | Requests served. Error rate is `rate(http_requests_total{status_code=~"5.."}[5m]) / rate(http_requests_total[5m])` |
| `cache_requests_total` | counter | `cache` (`search`/`answer`/`rerank`), `outcome` (`hit`/`miss`) | Cache effectiveness. A search coalesced into an in-flight identical query counts as a hit |
| `crawl_pages_total` | counter | `outcome` (`success`/`failure`) | Pages the crawler returned, blocked and failed ones included |
| `crawler_browser_sessions_active` | gauge | — | Browser sessions holding a slot in the concurrency pool right now (ceiling: `CRAWL_MAX_CONCURRENCY`) |
| `jobs_active` | gauge | `type` | Non-terminal background jobs, recomputed at scrape time |
| `llm_tokens_total` | counter | `label`, `kind` (`prompt`/`completion`) | Tokens billed per API-key label |
| `llm_cost_usd_total` | counter | `label` | Estimated spend per API-key label (same advisory figure as [Usage accounting](#usage-accounting)) |
| `research_pages_skipped_by_rerank_total` | counter | — | Pages the pre-crawl reranker dropped, i.e. crawls and tokens not paid for |

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/metrics
```

---

### POST /v1/search

Executes a web search via the bundled SearXNG instance and returns normalised results.

**Auth required:** When `AUTH_ENABLED=true`

**Request body** (`application/json`):

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `q` | `string` | Yes | — | Search query (min 1 character) |
| `categories` | `string[]` | No | `null` | SearXNG categories, e.g. `["general"]`, `["news"]` |
| `engines` | `string[]` | No | `null` | Restrict to specific engines, e.g. `["google", "bing"]` |
| `language` | `string` | No | `null` | Language code, e.g. `"en"`, `"fr"` |
| `pageno` | `integer` | No | `1` | Result page (≥ 1) |
| `time_range` | `"day"\|"week"\|"month"\|"year"` | No | `null` | Filter by publication date |
| `safesearch` | `0\|1\|2` | No | `null` | Safe search level (0=off, 1=moderate, 2=strict) |
| `max_results` | `integer` | No | `null` | Truncate to at most N results (1–100) |
| `include_domains` | `string[]` | No | `null` | Keep only results on these domains (subdomains included) |
| `exclude_domains` | `string[]` | No | `null` | Drop results on these domains (subdomains included) |

Domain filters are applied **before** `max_results`, so asking for 5 results on one domain
returns 5 of them rather than 5 minus whatever was filtered out. Host matching is the same
as the network guard's: `example.com` also matches `www.example.com`.

**Response `200`** — `SearchResponse`:

| Field | Type | Description |
|---|---|---|
| `query` | `string` | Original query string |
| `number_of_results` | `integer` | Total number of results returned |
| `results` | `SearchResultItem[]` | Array of search results |
| `suggestions` | `string[]` | Query refinement suggestions from SearXNG |
| `answers` | `string[]` | Instant answers (when SearXNG returns them) |

**`SearchResultItem`:**

| Field | Type | Description |
|---|---|---|
| `title` | `string` | Page title |
| `url` | `string` | Page URL |
| `snippet` | `string \| null` | Text excerpt |
| `engine` | `string \| null` | Source engine name |
| `score` | `float \| null` | Relevance score |
| `category` | `string \| null` | SearXNG category |
| `published_date` | `string \| null` | ISO 8601 date when available |

**Example:**

```bash
curl -X POST http://localhost:8000/v1/search \
  -H "Content-Type: application/json" \
  -d '{"q": "FastAPI tutorial", "max_results": 5}'
```

```json
{
  "query": "FastAPI tutorial",
  "number_of_results": 5,
  "results": [
    {
      "title": "FastAPI - A modern, fast web framework",
      "url": "https://fastapi.tiangolo.com",
      "snippet": "FastAPI framework, high performance, easy to learn...",
      "engine": "google",
      "score": 0.95,
      "category": "general",
      "published_date": null
    }
  ],
  "suggestions": ["fastapi documentation", "fastapi vs flask"],
  "answers": []
}
```

---

### GET /v1/search

Same search, same response body, as a cacheable `GET`. Exists so the endpoint can be tried
from a browser or curl, and — the real reason — so a reverse proxy or CDN in front of the API
can serve repeats without touching this process.

**Auth required:** When `AUTH_ENABLED=true`

**Query parameters:** every [`SearchRequest`](#searchrequest) field. List-valued parameters
(`categories`, `engines`, `include_domains`, `exclude_domains`) accept **either** repetition
**or** a comma-separated value — both forms below are equivalent:

```
GET /v1/search?q=fastapi&categories=general&categories=news
GET /v1/search?q=fastapi&categories=general,news
```

**Response headers:**

| Header | Value | Notes |
|---|---|---|
| `Cache-Control` | `public, max-age=60` | `private, max-age=60` when `AUTH_ENABLED=true`: an authenticated response must never be handed to another key by a shared cache |
| `ETag` | `"<sha256 prefix>"` | Hash of the response body |

Send the `ETag` back as `If-None-Match` to get a `304 Not Modified` with an empty body.

**Example:**

```bash
curl -i "http://localhost:8000/v1/search?q=FastAPI+tutorial&max_results=5&exclude_domains=pinterest.com"
```

`POST /v1/search` is unchanged and sets no cache headers.

---

### POST /v1/crawl

Crawls one or more URLs and returns their content as clean Markdown. Requests are executed concurrently, up to `CRAWL_MAX_CONCURRENCY` browser sessions shared across the whole process.

Every URL goes through the [network guard](#network-guard-ssrf) first: private and loopback targets
are refused. Results are returned in the order they were requested, one entry per requested URL.

**Auth required:** When `AUTH_ENABLED=true`

**Request body** (`application/json`):

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `urls` | `string[]` | Yes | — | URLs to crawl (1–50, must be valid HTTP/HTTPS URLs) |
| `formats` | `string[]` | No | `["markdown"]` | Which artefacts to return: any of `markdown`, `html`, `screenshot`, `pdf`, `links`, `tables`. Only what is asked for is computed and returned |
| `content_filter` | `"pruning"\|"none"` | No | `"pruning"` | Apply crawl4ai pruning filter for cleaner Markdown |
| `prune_threshold` | `float` | No | `0.48` | Aggressiveness of pruning (0.0–1.0) |
| `ignore_links` | `boolean` | No | `true` | Strip hyperlinks from Markdown output |
| `exclude_external_links` | `boolean` | No | `false` | Filter out external-domain links |
| `word_count_threshold` | `integer` | No | `0` | Drop content blocks with fewer than N words |
| `include_raw_markdown` | `boolean` | No | `false` | Include unfiltered Markdown alongside filtered output |
| `cache` | `boolean` | No | `true` | Use crawl4ai's built-in page cache |

**Response `200`** — `CrawlResponse`:

| Field | Type | Description |
|---|---|---|
| `count` | `integer` | Number of entries in `results`, i.e. one per input URL — failures included |
| `results` | `CrawledPage[]` | One entry per input URL |

**`CrawledPage`:**

| Field | Type | Description |
|---|---|---|
| `url` | `string` | The crawled URL |
| `success` | `boolean` | Whether crawling succeeded |
| `status_code` | `integer \| null` | HTTP status code returned by the page |
| `title` | `string \| null` | Page title |
| `markdown` | `string \| null` | Filtered Markdown content |
| `raw_markdown` | `string \| null` | Unfiltered Markdown (only when `include_raw_markdown=true`) |
| `word_count` | `integer` | Word count of the filtered Markdown |
| `internal_links` | `integer` | Number of internal links found on the page |
| `external_links` | `integer` | Number of external links found on the page |
| `error` | `string \| null` | Error message if `success=false` |
| `html` | `string \| null` | Cleaned HTML — only with `"html"` in `formats` |
| `screenshot` | `string \| null` | Base64 PNG — only with `"screenshot"` in `formats` |
| `pdf` | `string \| null` | Base64 PDF render — only with `"pdf"` in `formats` |
| `links` | `object \| null` | Full link map `{"internal": [...], "external": [...]}` — only with `"links"` in `formats` |
| `tables` | `array \| null` | Tables extracted from the page — only with `"tables"` in `formats` |

Fields not covered by the requested `formats` are `null`. The default (`["markdown"]`) returns
exactly what v0.1 returned, so existing callers see no change.

**PDF sources.** When `CRAWL_ENABLE_PDF=true` (the default), a URL that looks like a PDF
(`.pdf` suffix, or a `/pdf/` path segment for arXiv-style links) is downloaded and text-extracted
with `pypdf` instead of being rendered by the browser. The Markdown carries one `## Page n`
section per page, and `"pdf"` in `formats` returns the original bytes base64-encoded. A URL that
turns out not to serve `application/pdf` transparently falls back to the browser. PDFs over
`CRAWL_PDF_MAX_MB` are reported as a per-URL failure, never as a crash. `/v1/answer` and
`/v1/research` benefit automatically, since both crawl arbitrary search results.

**Example:**

```bash
curl -X POST http://localhost:8000/v1/crawl \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://example.com"],
    "content_filter": "pruning",
    "ignore_links": true
  }'
```

---

### POST /v1/crawl/site

Launches an asynchronous deep-crawl of an entire website using BFS link-following. Returns a job ID immediately; use `/v1/jobs/{id}` or `/v1/jobs/{id}/stream` to track progress.

**Auth required:** When `AUTH_ENABLED=true`

**Request body** (`application/json`):

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `url` | `string` | Yes | — | Entry-point URL |
| `max_depth` | `integer` | No | `2` | Maximum link-following depth from the entry point (0–5) |
| `max_pages` | `integer` | No | `25` | Hard cap on total pages crawled (1–500) |
| `include_external` | `boolean` | No | `false` | Follow links to external domains |
| `url_patterns` | `string[]` | No | `null` | Glob patterns to restrict which URLs are followed |
| `content_filter` | `"pruning"\|"none"` | No | `"pruning"` | Apply pruning filter |

**Response `202`** — `JobSubmitted`:

| Field | Type | Description |
|---|---|---|
| `job_id` | `string` | UUID to poll or stream |
| `type` | `string` | Job type — `"site_crawl"` here |
| `status` | `"pending"` | Initial job status |
| `stream_url` | `string` | Ready-made path of the job's SSE stream |

**Example:**

```bash
curl -X POST http://localhost:8000/v1/crawl/site \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://docs.example.com",
    "max_depth": 2,
    "max_pages": 50
  }'
```

```json
{
  "job_id": "a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5",
  "type": "site_crawl",
  "status": "pending",
  "stream_url": "/v1/jobs/a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5/stream"
}
```

When the job completes, `result` in `/v1/jobs/{id}` is `{"base_url": ..., "count": ..., "pages": CrawledPage[]}`.

---

### POST /v1/extract

Extracts structured data from a list of URLs. Two modes:

| Mode | How | Cost |
|---|---|---|
| `css` | crawl4ai's `JsonCssExtractionStrategy` — deterministic CSS selectors | No LLM call |
| `llm` | The page Markdown is handed to the model with a Pydantic schema built at request time from your JSON Schema | One model call per URL |

URLs go through the same crawl path as `/v1/crawl`, so the same [network guard](#network-guard-ssrf),
per-host politeness and global browser budget apply. A failure on one URL is reported on that URL
only; the rest of the batch still returns.

**Auth required:** When `AUTH_ENABLED=true`

**Request body** (`application/json`):

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `urls` | `string[]` | Yes | — | URLs to extract from (1–25; capped lower than `/v1/crawl` because `llm` mode costs a model call each) |
| `schema` | `object` | Yes | — | In `llm` mode a **JSON Schema** object; in `css` mode a **crawl4ai CSS schema** (`{"name", "baseSelector", "fields": [...]}`) |
| `mode` | `"llm"\|"css"` | No | `"llm"` | Extraction strategy |
| `model` | `string` | No | `null` | litellm model override (`llm` mode only) |

The JSON-Schema mapping is reasonable-effort, not a full implementation: `string`, `number`,
`integer`, `boolean`, arrays, nested objects and type unions are supported; anything else becomes
an untyped field rather than being dropped. Fields outside `required` are optional, so a page
missing one still yields the rest.

**Response `200`** — `ExtractResponse`:

| Field | Type | Description |
|---|---|---|
| `count` | `integer` | Number of entries (one per input URL) |
| `results` | `ExtractResult[]` | One entry per input URL, in request order |

**`ExtractResult`:** `url` (`string`), `data` (`object \| null`), `success` (`boolean`), `error` (`string \| null`).

In `css` mode crawl4ai returns a *list* of records: a single record is returned as-is, several are
wrapped as `{"items": [...]}`.

**Example:**

```bash
curl -X POST http://localhost:8000/v1/extract \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://example.com/product/1"],
    "mode": "llm",
    "schema": {
      "type": "object",
      "properties": {
        "title": {"type": "string"},
        "price": {"type": "number"}
      },
      "required": ["title"]
    }
  }'
```

```json
{
  "count": 1,
  "results": [
    { "url": "https://example.com/product/1", "data": { "title": "Widget", "price": 9.5 }, "success": true, "error": null }
  ]
}
```

**Errors:** `400` when every URL is refused by the network guard, `422` when the schema is unusable.

---

### POST /v1/map

Discovers a site's URLs in about a second, without crawling it. Useful as a pre-step to
`/v1/crawl/site`: map first, then crawl only what you want.

Three sources are tried in order, and the first that yields URLs wins:

1. `{origin}/sitemap.xml` — including one level of sitemap-index nesting (at most 10 nested sitemaps, to bound amplification)
2. `Sitemap:` directives in `{origin}/robots.txt`
3. the links of the entry page itself, crawled once

The entry URL passes the [network guard](#network-guard-ssrf) before anything is fetched, and so
does every document a sitemap or robots.txt points at.

**Auth required:** When `AUTH_ENABLED=true`

**Request body** (`application/json`):

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `url` | `string` | Yes | — | Any URL on the site; its origin is the entry point |
| `search` | `string` | No | `null` | Keep only URLs containing this substring (case-insensitive) |
| `limit` | `integer` | No | `100` | Max URLs returned (1–1000) |

**Response `200`** — `MapResponse`:

| Field | Type | Description |
|---|---|---|
| `links` | `string[]` | Discovered URLs, deduplicated, filtered and truncated |
| `count` | `integer` | Number of URLs returned |
| `source` | `"sitemap"\|"robots"\|"page"` | Which source produced them |

**Example:**

```bash
curl -X POST http://localhost:8000/v1/map \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "search": "pricing", "limit": 200}'
```

```json
{ "links": ["https://example.com/pricing"], "count": 1, "source": "sitemap" }
```

---

### POST /v1/answer

Answers a question in a single pipeline pass: search → optional full-page crawl → LLM synthesis with numbered `[n]` citations. Faster than `/v1/research` but shallower.

**Auth required:** When `AUTH_ENABLED=true`

**Request body** (`application/json`):

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | `string` | Yes | — | Question to answer (min 1 character) |
| `max_sources` | `integer` | No | `5` | Number of sources to retrieve and consult (1–15) |
| `model` | `string` | No | `null` | litellm model string override (e.g. `"anthropic/claude-3-5-haiku-20241022"`) |
| `language` | `string` | No | `null` | Language hint passed to SearXNG |
| `categories` | `string[]` | No | `null` | SearXNG categories to search in |
| `fetch_full` | `boolean` | No | `true` | Crawl full page content; `false` uses snippets only (faster, less accurate) |
| `stream` | `boolean` | No | `false` | Return `text/event-stream` instead of JSON — see below |

**Streaming.** With `stream: true` the response is a Server-Sent Event stream of named events:
`searching` (`{query}`), `crawling` (`{count}`), `generating` (`{}`), then one `token` event per
generated token (`{token}`), and finally `done` carrying `{model, sources, usage}` — the same
`sources` and `usage` the JSON response returns. A failure mid-stream arrives as an `error` event
(`{detail}`) rather than an HTTP status, since the headers are already sent.

**Caching.** Non-streaming answers are cached in-process for `ANSWER_CACHE_TTL` seconds, keyed by
`query + model + max_sources + language`. A repeat within the window re-runs neither the search,
the crawl, nor the model. Streaming answers (`stream=true`) are neither served from nor written to
the cache: the point of streaming is the first token arriving early. Set `ANSWER_CACHE_ENABLED=false`
to turn it off.

**Response `200`** — `AnswerResponse`:

| Field | Type | Description |
|---|---|---|
| `query` | `string` | Original question |
| `answer` | `string` | LLM-synthesised answer with `[1]`, `[2]` … inline citations |
| `model` | `string` | LLM model that generated the answer |
| `sources` | `Source[]` | Ordered list of consulted sources |
| `usage` | `Usage` | Tokens, estimated cost, searches and pages for this call — see [Cost controls](#cost-controls). All zeros on a cache hit |

**`Source`:**

| Field | Type | Description |
|---|---|---|
| `index` | `integer` | Citation index (matches `[n]` in the answer) |
| `title` | `string` | Page title |
| `url` | `string` | Source URL |
| `snippet` | `string \| null` | Excerpt used |
| `used_full_text` | `boolean` | Whether the full crawled page was used (vs. snippet only) |

**Example:**

```bash
curl -X POST http://localhost:8000/v1/answer \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is the capital of Australia?",
    "max_sources": 3
  }'
```

```json
{
  "query": "What is the capital of Australia?",
  "answer": "The capital of Australia is Canberra [1]. Despite common belief, it is not Sydney [2].",
  "model": "openai/gpt-4o-mini",
  "sources": [
    {
      "index": 1,
      "title": "Capital of Australia – Wikipedia",
      "url": "https://en.wikipedia.org/wiki/Canberra",
      "snippet": "Canberra is the capital city of Australia...",
      "used_full_text": true
    },
    {
      "index": 2,
      "title": "Common misconceptions about Australian geography",
      "url": "https://example.com/australia",
      "snippet": null,
      "used_full_text": false
    }
  ]
}
```

---

### POST /v1/research

Runs a multi-step deep-research pipeline: generates SERP queries, searches, crawls, extracts learnings recursively, then writes a structured Markdown report. Can run synchronously (default) or as a background job.

**Auth required:** When `AUTH_ENABLED=true`

**Request body** (`application/json`):

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `query` | `string` | Yes | — | Research topic or question (min 1 character) |
| `breadth` | `integer` | No | config value | Number of SERP queries generated per recursion level (1–10) |
| `depth` | `integer` | No | config value | Number of recursion levels (1–5) |
| `model` | `string` | No | `null` | litellm model override |
| `interactive` | `boolean \| "auto"` | No | `"auto"` | Clarification behaviour: `false` = skip, `true` = always ask, `"auto"` = model decides |
| `answers` | `string[]` | No | `null` | Answers to clarifying questions from a previous `needs_clarification` response; providing this triggers the actual research run |
| `background` | `boolean` | No | `false` | Return immediately with a job ID instead of waiting for completion |
| `stream` | `boolean` | No | `false` | Stream the final report token by token over the job's SSE channel. **Implies `background=true`** — `GET /v1/jobs/{id}/stream` is the only SSE channel |
| `time_range` | `"day"\|"week"\|"month"\|"year"` | No | `null` | Restrict every SERP query of the run to this window (also feeds the reranker's freshness bonus) |
| `include_domains` | `string[]` | No | `null` | Only research sources on these domains (subdomains included) |
| `exclude_domains` | `string[]` | No | `null` | Never research sources on these domains (subdomains included) |
| `estimate_only` | `boolean` | No | `false` | Return a projected cost and page count **without launching anything** — no search, no crawl, no model call. Answers with `status: "estimated"` |
| `webhook_url` | `string` | No | `null` | Signed callback POSTed when a background job finishes — see [Webhooks](#webhooks). Requires `background`/`stream` and a configured `WEBHOOK_SECRET` |

The filters apply at **every** recursion level, and domains are filtered before ranking and
crawling — a filtered-out result never costs a crawl or a token.

**Response — three possible shapes:**

#### 1. `needs_clarification` (when `interactive` ≠ `false` and the model needs more context)

```json
{
  "status": "needs_clarification",
  "query": "quantum computing",
  "model": "openai/gpt-4o-mini",
  "questions": [
    "Are you interested in the hardware aspects or the algorithmic side?",
    "What is your background level — beginner, intermediate, or expert?"
  ]
}
```

The clarification round-trip is itself a model call, so this response also carries a `usage` object,
and what it spent is billed to the caller's daily budget.

Re-submit with `answers` to proceed:

```json
{
  "query": "quantum computing",
  "answers": ["algorithmic side", "intermediate"],
  "interactive": false
}
```

#### 2. `completed` (synchronous run)

| Field | Type | Description |
|---|---|---|
| `status` | `"completed"` | Completion marker |
| `query` | `string` | Original query |
| `model` | `string` | LLM model used |
| `report` | `string` | Full Markdown research report (typically 3+ pages) with inline `[n]` citations |
| `learnings` | `string[]` | Key facts extracted during research |
| `sources` | `string[]` | Deduplicated list of source URLs consulted |
| `citations` | `{index, url}[]` | Numbered source map the report's `[n]` markers refer to |
| `serp_queries` | `string[]` | All search queries generated during the run |
| `usage` | `Usage` | What the run cost — see [Cost controls](#cost-controls) |
| `stopped_early` | `boolean` | `true` when `RESEARCH_MAX_COST_USD` cut the exploration short. The report is still complete and self-describing; it just covers less ground |

#### 3. `JobSubmitted` (when `background: true`)

```json
{
  "job_id": "a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5",
  "type": "research",
  "status": "pending",
  "stream_url": "/v1/jobs/a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5/stream"
}
```

Track with `/v1/jobs/{id}`, or pass a [`webhook_url`](#webhooks) to be told instead of polling. The final
`result` mirrors the `completed` shape above, `usage` included.

#### 4. `estimated` (when `estimate_only: true`)

Prices the run from the request alone and returns before anything is launched.

```json
{
  "status": "estimated",
  "query": "quantum computing",
  "model": "openai/gpt-4o-mini",
  "estimate": {
    "serp_queries": 12,
    "searches": 12,
    "pages_crawled": 36,
    "llm_calls": 30,
    "prompt_tokens": 117200,
    "completion_tokens": 9700,
    "estimated_cost_usd": 0.0234,
    "model": "openai/gpt-4o-mini"
  }
}
```

The fanout (`serp_queries`, `pages_crawled`, `llm_calls`) is exact — it replays the engine's own
recursion. The token and dollar figures are averages, so expect the cost to be right to within roughly a
factor of two: enough to answer "cents or dollars?", which is the question it exists to answer.

**Example — synchronous research:**

```bash
curl -X POST http://localhost:8000/v1/research \
  -H "Content-Type: application/json" \
  -d '{
    "query": "State of AI coding assistants in 2025",
    "breadth": 4,
    "depth": 2,
    "interactive": false
  }'
```

**Example — background research:**

```bash
curl -X POST http://localhost:8000/v1/research \
  -H "Content-Type: application/json" \
  -d '{
    "query": "State of AI coding assistants in 2025",
    "background": true,
    "interactive": false
  }'
```

---

### GET /v1/jobs/{job\_id}

Returns the current state of an async job (site crawl or background research).

**Auth required:** When `AUTH_ENABLED=true`

**Path parameters:**

| Parameter | Type | Description |
|---|---|---|
| `job_id` | `string` | Job ID returned by a previous `202` response |

**Response `200`** — `JobView`:

| Field | Type | Description |
|---|---|---|
| `id` | `string` | Job UUID |
| `type` | `string` | Job type: `"site_crawl"` or `"research"` |
| `status` | `"pending"\|"running"\|"completed"\|"failed"` | Current state |
| `created_at` | `string` | ISO 8601 creation timestamp |
| `updated_at` | `string` | ISO 8601 last-update timestamp |
| `progress` | `object[]` | Ordered list of progress events emitted so far |
| `result` | `object \| null` | Final result payload (present when `status=completed`) |
| `error` | `string \| null` | Error message (present when `status=failed`) |

**Response `404`** when `job_id` is unknown.

**Example:**

```bash
curl http://localhost:8000/v1/jobs/a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5
```

---

### DELETE /v1/jobs/{job\_id}

Cancels a running job. Cancellation is requested, not awaited: the response returns immediately and
the job settles as `failed` with `error: "cancelled"` once the task unwinds.

**Auth required:** When `AUTH_ENABLED=true`

**Response `202`:**

```json
{ "job_id": "a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5", "status": "cancelling" }
```

**Response `404`** when `job_id` is unknown, **`409`** when the job already reached a terminal state.

---

### GET /v1/jobs/{job\_id}/stream

Streams real-time progress events for a job as [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events). Replays past events immediately for clients that connect after the job has started, then delivers new events as they arrive. The stream closes when the job reaches a terminal state.

**Auth required:** When `AUTH_ENABLED=true`

**Path parameters:** same as `/v1/jobs/{job_id}`

**Response:** `text/event-stream` — see [SSE streaming format](#sse-streaming-format) below.

**Example (curl):**

```bash
curl -N http://localhost:8000/v1/jobs/a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5/stream
```

**Example (JavaScript):**

```js
const es = new EventSource('/v1/jobs/JOB_ID/stream');
// Events are *named* (`event: progress`), so `onmessage` never fires — listen per name.
es.addEventListener('progress', (e) => console.log(JSON.parse(e.data)));
es.addEventListener('completed', () => es.close());
es.addEventListener('failed', (e) => { console.error(JSON.parse(e.data).error); es.close(); });
```

---

## Schemas

### SearchRequest

```json
{
  "q": "string",
  "categories": ["string"] | null,
  "engines": ["string"] | null,
  "language": "string" | null,
  "pageno": 1,
  "time_range": "day" | "week" | "month" | "year" | null,
  "safesearch": 0 | 1 | 2 | null,
  "max_results": null,
  "include_domains": ["string"] | null,
  "exclude_domains": ["string"] | null
}
```

### CrawlRequest

```json
{
  "urls": ["https://example.com"],
  "formats": ["markdown"],
  "content_filter": "pruning",
  "prune_threshold": 0.48,
  "ignore_links": true,
  "exclude_external_links": false,
  "word_count_threshold": 0,
  "include_raw_markdown": false,
  "cache": true
}
```

### SiteCrawlRequest

```json
{
  "url": "https://example.com",
  "max_depth": 2,
  "max_pages": 25,
  "include_external": false,
  "url_patterns": null,
  "content_filter": "pruning"
}
```

### ExtractRequest

```json
{
  "urls": ["https://example.com"],
  "schema": { "type": "object", "properties": { "title": { "type": "string" } } },
  "mode": "llm",
  "model": null
}
```

### MapRequest

```json
{
  "url": "https://example.com",
  "search": null,
  "limit": 100
}
```

### AnswerRequest

```json
{
  "query": "string",
  "max_sources": 5,
  "model": null,
  "language": null,
  "categories": null,
  "fetch_full": true,
  "stream": false
}
```

### ResearchRequest

```json
{
  "query": "string",
  "breadth": null,
  "depth": null,
  "model": null,
  "interactive": "auto",
  "answers": null,
  "background": false,
  "stream": false,
  "time_range": null,
  "include_domains": null,
  "exclude_domains": null,
  "estimate_only": false,
  "webhook_url": null
}
```

### Usage

Returned by `/v1/answer` and `/v1/research`. See [Cost controls](#cost-controls).

```json
{
  "prompt_tokens": 48210,
  "completion_tokens": 3120,
  "total_cost_usd": 0.021,
  "searches": 12,
  "pages_crawled": 27,
  "pages_skipped_by_rerank": 43
}
```

---

## Configuration reference

All settings are environment variables (case-insensitive). Copy `.env.example` to `.env` and edit.

### App

| Variable | Default | Description |
|---|---|---|
| `APP_NAME` | `Unified Search API` | Name shown in API metadata |
| `ENVIRONMENT` | `development` | Deployment environment label |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FORMAT` | `json` | `json` for one structured object per line (see [Logging](#logging)), `text` for the plain one-liner |
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry tracing of FastAPI and httpx. The collector endpoint comes from the OTel SDK's own `OTEL_EXPORTER_OTLP_ENDPOINT` |

### Authentication

| Variable | Default | Description |
|---|---|---|
| `AUTH_ENABLED` | `true` | Require API key on all `/v1/*` routes. Startup fails if `API_KEYS` is empty |
| `API_KEYS` | `""` | Comma-separated list of valid API keys. Each entry is `label:key` or a bare `key` (`openssl rand -hex 32`) — see [Labeled keys](#labeled-keys) |

### Hardening

| Variable | Default | Description |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | Enable weighted rate limiting |
| `RATE_LIMIT_RPM` | `120` | Points per 60s window per caller (keyed by API-key label, or by IP when auth is off) |
| `RATE_LIMIT_RPM_OVERRIDES` | `""` | Per-label budgets, e.g. `app-mobile:500,test:20` |
| `RATE_LIMIT_MAX_CLIENTS` | `10000` | Max distinct clients tracked (coldest evicted first) |
| `TRUSTED_PROXY` | `false` | Read the client IP from `X-Forwarded-For`. Enable **only** behind a proxy that sets it, or a caller can spoof the header and dodge the IP-keyed limiter |
| `SECURITY_HEADERS` | `true` | Add `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` and a default `Cache-Control: no-store` |
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed CORS origins |

### Network guard (SSRF)

Every URL the crawler is given is checked before the browser connects, and again on the URL the
crawl landed on (so a redirect cannot reach the internal network). Hostnames are resolved first:
loopback, private, link-local and CGNAT addresses are refused, as are non-`http(s)` schemes.
A request whose URLs are *all* refused returns `400`; in a mixed batch the refused URLs come back
as `success: false` with a `blocked: …` error.

| Variable | Default | Description |
|---|---|---|
| `CRAWL_ALLOW_PRIVATE_NETWORKS` | `false` | Allow crawling private/loopback addresses. Never enable in production |
| `CRAWL_ALLOWED_PORTS` | `80,443,8080` | Ports the crawler may connect to |
| `CRAWL_DOMAIN_ALLOWLIST` | `""` | If set, only these domains (and subdomains) may be crawled |
| `CRAWL_DOMAIN_DENYLIST` | `""` | Domains (and subdomains) never fetched |

### SearXNG

| Variable | Default | Description |
|---|---|---|
| `SEARXNG_URL` | `http://searxng:8080` | SearXNG instance base URL |
| `SEARXNG_TIMEOUT` | `15.0` | Request timeout in seconds |
| `SEARXNG_MAX_CONNECTIONS` | `64` | Max pooled HTTP connections kept open to SearXNG |
| `SEARCH_CACHE_ENABLED` | `true` | Memoize identical search queries in-process |
| `SEARCH_CACHE_TTL` | `300` | Seconds an identical search result is reused |
| `SEARCH_CACHE_MAX_SIZE` | `512` | Max distinct queries kept (LRU eviction) |

### LLM

| Variable | Default | Description |
|---|---|---|
| `LLM_MODEL` | `openai/gpt-4o-mini` | Default model (litellm provider string) |
| `LLM_REPORT_MODEL` | `""` | Optional dedicated model for research reports (falls back to `LLM_MODEL`) |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature |
| `LLM_MAX_TOKENS` | `4096` | Maximum output tokens |
| `LLM_REPORT_MAX_TOKENS` | `8192` | Maximum output tokens for the final research report |
| `LLM_FALLBACK_MODELS` | `""` | Comma-separated models tried in order when the primary keeps failing |
| `LLM_TIMEOUT` | `120.0` | Max seconds for a single non-streaming LLM call |
| `LLM_STREAM_TIMEOUT` | `30.0` | Max seconds a streaming call may stall before it is retried |
| `LLM_MAX_RETRIES` | `3` | Retries per model before the next fallback model is tried |
| `LLM_MAX_CONCURRENCY` | `10` | Process-wide cap on in-flight LLM calls |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `OPENROUTER_API_KEY` | — | OpenRouter API key |

Any other provider API key recognised by [litellm](https://docs.litellm.ai/docs/providers) can be added.

### Crawling

| Variable | Default | Description |
|---|---|---|
| `CRAWL_MAX_CONCURRENCY` | `5` | Max concurrent browser sessions, **process-wide** (not per request) |
| `CRAWL_TIMEOUT` | `30.0` | Per-page crawl timeout in seconds |
| `CRAWL_PER_HOST_DELAY` | `1.0` | Min seconds between two hits on the same host; distinct hosts are never delayed |
| `CRAWL_ENABLE_PDF` | `true` | Route PDF URLs through `pypdf` text extraction instead of the browser |
| `CRAWL_PDF_MAX_MB` | `25` | Refuse PDFs larger than this (reported as a per-URL failure) |

### URL discovery

| Variable | Default | Description |
|---|---|---|
| `MAP_TIMEOUT` | `5.0` | Max seconds per `sitemap.xml` / `robots.txt` fetch in `/v1/map` |

### Answer cache

| Variable | Default | Description |
|---|---|---|
| `ANSWER_CACHE_ENABLED` | `true` | Reuse identical non-streaming `/v1/answer` responses |
| `ANSWER_CACHE_TTL` | `600` | Seconds an entry is reused (`0` disables the cache) |
| `ANSWER_CACHE_MAX_SIZE` | `256` | Max distinct answers kept (LRU eviction) |

### Jobs

| Variable | Default | Description |
|---|---|---|
| `JOB_BACKEND` | `sqlite` | `sqlite` persists jobs, results and progress events across a restart (needs the `./data` volume); `memory` keeps everything in-process and loses it on restart |
| `JOB_DB_PATH` | `/srv/api/data/jobs.db` | SQLite file for `JOB_BACKEND=sqlite`. Its directory is created on first use |
| `JOB_VACUUM_INTERVAL_SECONDS` | `86400` | How often the job database is `VACUUM`ed (`0` disables) |
| `JOB_RETENTION_SECONDS` | `3600` | Seconds a finished job and its result are kept. Wall-clock, so it keeps counting across a restart |
| `JOB_EVICT_INTERVAL_SECONDS` | `300` | Sweep interval for aged-out jobs (`0` disables) |
| `JOB_MAX_PROGRESS_EVENTS` | `500` | Progress events retained per job (oldest dropped) |
| `JOB_SUBSCRIBER_QUEUE_SIZE` | `1000` | Buffered events per SSE subscriber; a slow client loses the oldest |

### Research

| Variable | Default | Description |
|---|---|---|
| `RESEARCH_DEFAULT_BREADTH` | `4` | Default SERP queries per recursion level |
| `RESEARCH_DEFAULT_DEPTH` | `2` | Default recursion depth |
| `RESEARCH_CONCURRENCY` | `2` | Parallel query workers, **for the whole run** (not per recursion level) |
| `RESEARCH_SYNC_TIMEOUT` | `600.0` | Max wall-clock seconds for a synchronous run before a `504` (`0` = no limit). Use `background: true` for long queries |
| `RESEARCH_REPORT_TOKEN_BUDGET` | `12000` | Token budget for the learnings fed to the final-report prompt |
| `RESEARCH_RESULTS_PER_QUERY` | `10` | SERP results requested per query (titles/snippets are free) |
| `RESEARCH_CRAWL_TOP_K` | `3` | Reranked results per query that are actually crawled |
| `RESEARCH_RERANK` | `llm` | Pre-crawl relevance filter: `off`, `lexical`, or `llm` |
| `RESEARCH_RERANK_MODEL` | `""` | Model used by the LLM reranker (falls back to `LLM_MODEL`) |
| `RESEARCH_RERANK_CACHE_TTL` | `600` | Seconds a rerank decision is reused across overlapping SERPs (`0` disables) |
| `RESEARCH_PER_PAGE_TOKEN_BUDGET` | `2000` | Max tokens kept from a single crawled page |
| `RESEARCH_PROCESS_CONTEXT_TOKEN_BUDGET` | `8000` | Token budget for the pages fed to one extraction call |
| `RESEARCH_MAX_COST_USD` | `0` | Per-run cost ceiling; the run stops exploring and writes its report (`0` disables) |
| `RESEARCH_NUM_LEARNINGS` | `3` | Max learnings extracted per SERP query |
| `RESEARCH_NUM_FOLLOWUPS` | `3` | Max follow-up questions extracted per SERP query |

### Cost controls

| Variable | Default | Description |
|---|---|---|
| `DAILY_COST_CAP_USD` | `0` | Max USD per API-key label per UTC day across `/v1/research` and `/v1/answer` (`0` disables). Durable with `JOB_BACKEND=sqlite`; resets on restart with `JOB_BACKEND=memory` |
| `RESEARCH_MAX_COST_USD` | `0` | Per-run ceiling — see the Research table above |

### Webhooks

| Variable | Default | Description |
|---|---|---|
| `WEBHOOK_SECRET` | `""` | HMAC-SHA256 key for `X-Webhook-Signature` (`openssl rand -hex 32`). While empty, requests carrying a `webhook_url` are refused with a `400` |
| `WEBHOOK_TIMEOUT` | `10.0` | Max seconds for the callback POST. A failing webhook never fails the job |

---

## SSE streaming format

The `/v1/jobs/{id}/stream` endpoint uses Server-Sent Events. Each event is delivered as a named
event whose payload is a JSON object — `event: <name>\ndata: <json>\n\n` — and the name is repeated
inside the JSON as `event`, so a client can dispatch on either.

### Event types

#### `status`

Emitted once when the job transitions to `running`.

```json
{ "event": "status", "status": "running" }
```

#### `progress`

Emitted throughout the job with context-specific fields.

**For research jobs:**

```json
{
  "event": "progress",
  "stage": "searching",
  "depth": "1/2",
  "queries": "3/8",
  "message": "quantum error correction"
}
```

```json
{
  "event": "progress",
  "stage": "processed",
  "depth": "1/2",
  "queries": "4/8",
  "message": "quantum error correction"
}
```

With `stream: true` on `/v1/research`, the final report also arrives token by token on the same
channel. Token events reuse the `progress` envelope and are discriminated by `stage`:

```json
{ "event": "progress", "stage": "report_token", "token": "## Findings" }
```

Concatenating every `report_token` in order reproduces the report body; the full report (with its
numbered source appendix) is still available from `GET /v1/jobs/{id}` once the job completes.

**For site crawl jobs:**

```json
{
  "event": "progress",
  "stage": "crawled",
  "pages": 12,
  "url": "https://docs.example.com/guide"
}
```

#### `completed`

Emitted once when the job finishes successfully. Fetch the full result via `GET /v1/jobs/{id}`.

```json
{ "event": "completed" }
```

#### `failed`

Emitted if the job encounters an unrecoverable error.

```json
{ "event": "failed", "error": "SearXNG returned 503" }
```

#### `__end__`

Internal sentinel that closes the stream. Clients can safely ignore this or use it to close the `EventSource`.

```json
{ "event": "__end__" }
```

### Full SSE stream example

```
event: status
data: {"event": "status", "status": "running"}

event: progress
data: {"event": "progress", "stage": "searching", "depth": "1/2", "queries": "0/4", "message": "AI coding tools 2025"}

event: progress
data: {"event": "progress", "stage": "processed", "depth": "1/2", "queries": "1/4", "message": "AI coding tools 2025"}

event: progress
data: {"event": "progress", "stage": "report_token", "token": "# AI coding tools"}

event: completed
data: {"event": "completed"}

event: __end__
data: {"event": "__end__"}
```
