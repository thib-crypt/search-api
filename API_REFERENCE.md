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
  - [POST /v1/convert](#post-v1convert)
  - [POST /v1/answer](#post-v1answer)
  - [POST /v1/research](#post-v1research)
  - [GET /v1/jobs/{job\_id}](#get-v1jobsjob_id)
  - [DELETE /v1/jobs/{job\_id}](#delete-v1jobsjob_id)
  - [GET /v1/jobs/{job\_id}/stream](#get-v1jobsjob_idstream)
  - [POST /mcp](#post-mcp)
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
| `/v1/map`, `/v1/convert` | 2 |
| `/v1/crawl` | 5 |
| `/v1/answer`, `/v1/extract` | 10 |
| `/v1/crawl/site` | 25 |
| `/v1/research` | 100 |
| anything else (e.g. `/v1/jobs/*`) | 1 |

**MCP tools are priced the same way**, from the same table: the same work costs the same
points whether it was asked for over HTTP or over `/mcp`.

| Tool | Weight |
|---|---|
| `web_search`, `deep_research_estimate`, `job_status` | 1 |
| `map_site` | 2 |
| `fetch_url` | 5 |
| `answer`, `extract_structured` | 10 |
| `crawl_site` | 25 |
| `deep_research` | 100 |

This takes two steps, because the middleware runs *before* routing and sees only the path
`/mcp` — every tool would otherwise cost 1 point. It charges that 1 point on the way in, and
the MCP layer debits the remainder once it knows which tool was called, against the same
sliding window. A refused call is not charged at all, and the refusal tells the agent what
the tool costs and when the budget frees up, so it can pick a cheaper tool instead of
retrying into the same wall. The stdio transport has no window to draw from and is not
rate-limited.

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
  "endpoints": ["/v1/search", "/v1/crawl", "/v1/crawl/site", "/v1/extract", "/v1/map", "/v1/convert", "/v1/answer", "/v1/research", "/v1/jobs/{id}"],
  "mcp": "/mcp"
}
```

`mcp` is the path the [MCP server](#post-mcp) is served on, or `null` when `MCP_ENABLED=false` —
so an agent client can discover the transport without being told where it lives.

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
| `crawl_pages_total` | counter | `outcome` (`success`/`failure`), `source_format` | Pages the crawler returned, blocked and failed ones included. `source_format` is `html` for a browser-rendered page, the document format (`pdf`, `docx`, `xlsx`, …) for an ingested one, or `unknown` for a page that failed before anything was read — so `sum by (source_format)` shows how much of a run came out of office documents |
| `crawler_browser_sessions_active` | gauge | — | Browser sessions holding a slot in the concurrency pool right now (ceiling: `CRAWL_MAX_CONCURRENCY`) |
| `jobs_active` | gauge | `type` | Non-terminal background jobs, recomputed at scrape time |
| `llm_tokens_total` | counter | `label`, `kind` (`prompt`/`completion`) | Tokens billed per API-key label |
| `llm_cost_usd_total` | counter | `label` | Estimated spend per API-key label (same advisory figure as [Usage accounting](#usage-accounting)) |
| `research_pages_skipped_by_rerank_total` | counter | — | Pages the pre-crawl reranker dropped, i.e. crawls and tokens not paid for |
| `mcp_tool_calls_total` | counter | `tool`, `label`, `outcome` (`success`/`error`/`refused`) | MCP tool calls. `refused` is a call the rate limiter, the daily cap or the research concurrency bound turned away **before** it spent anything — the series worth alerting on |
| `mcp_tool_duration_seconds` | histogram | `tool` | Wall-clock duration of a tool call. A `crawl_site` or `deep_research` call returns a job id, so its duration is the submission, not the run |
| `mcp_result_tokens` | histogram | `tool` | Tokens of text handed back **after** budgeting — the check that `MCP_MAX_RESULT_TOKENS` survives contact with real pages |
| `mcp_artifacts_stored` | gauge | — | Full tool payloads currently held in the artifact store (ceiling: `MCP_ARTIFACT_MAX_ITEMS`) |

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
| `source_format` | `string \| null` | What the content was read from: `"html"` for a browser-rendered page, or the document format (`"pdf"`, `"docx"`, `"xlsx"`, …) |
| `html` | `string \| null` | Cleaned HTML — only with `"html"` in `formats` |
| `screenshot` | `string \| null` | Base64 PNG — only with `"screenshot"` in `formats` |
| `pdf` | `string \| null` | Base64 PDF render — only with `"pdf"` in `formats` |
| `links` | `object \| null` | Full link map `{"internal": [...], "external": [...]}` — only with `"links"` in `formats` |
| `tables` | `array \| null` | Tables extracted from the page — only with `"tables"` in `formats` |

Fields not covered by the requested `formats` are `null`. The default (`["markdown"]`) returns
exactly what v0.1 returned, so existing callers see no change.

**Document sources.** When `CRAWL_ENABLE_DOCUMENTS=true` (the default), a URL that serves a
document is downloaded and converted to Markdown instead of being handed to the browser, which
would only download it again and render an empty page. Word, PowerPoint, Excel, OpenDocument,
RTF, EPUB, CSV and PDF are covered; **tables come back as Markdown tables**, headings as
headings, lists with their original numbering.

The format is decided by the **bytes**, in this order:

1. the file's signature (PDF header, RTF open group, OLE stream names, ZIP package mimetype);
2. the `Content-Type` header, as a tie-breaker only;
3. the URL's extension — the only evidence for a signature-less format such as CSV.

That ordering is deliberate: document portals mislabel downloads constantly, so a real `.docx`
served as `application/octet-stream` is read correctly, and an HTML error page served as
`application/pdf` is not. A URL that turns out not to serve a document falls back to the browser
transparently. A URL with no extension at all (`/download?id=4021`) is given a **second chance**:
if the browser returns a successful but wordless page, the document path is tried once before the
result is returned.

`source_format` on each result says what was read. `"pdf"` in `formats` returns the original
bytes base64-encoded, and only for an actual PDF. Documents over `CRAWL_DOC_MAX_MB` are reported
as a per-URL failure, never as a crash; so are unreadable ones, with the reason named
(`EncryptedError` for a password-protected file). `/v1/extract`, `/v1/answer` and `/v1/research`
benefit automatically, since all three crawl arbitrary search results.

**No OCR.** A scanned PDF — an image with no text layer — has nothing to extract, and comes back
as an explicit failure rather than a silently empty page. `CRAWL_PDF_FALLBACK=true` (the default)
additionally retries PDFs through `pypdf` when the converter fails on them, which keeps the
pre-Phase-15 extraction path one environment variable away.

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

### POST /v1/convert

Converts an uploaded document to Markdown. Same converter as the crawl path, applied to a file you
send directly instead of one fetched from a URL.

`Content-Type: multipart/form-data`.

| Field | Type | Required | Description |
|---|---|---|---|
| `file` | file | Yes | The document to convert |
| `format` | `string` | No | Names the format instead of letting it be detected. Required for CSV, which carries no signature in its bytes. One of `doc`, `docx`, `odt`, `pdf`, `ppt`, `pptx`, `rtf`, `epub`, `xlsx`, `ods`, `odp`, `csv` |

**Response**

| Field | Type | Description |
|---|---|---|
| `markdown` | `string` | GitHub-Flavored Markdown rendering of the document |
| `format` | `string` | The format the document was actually read as. May differ from `format` in the request when the bytes said otherwise, and from the file's extension for container variants (a `.docm` reads as `docx`) |
| `title` | `string \| null` | The document's first heading, or `null` when it opens without one |
| `word_count` | `integer` | Words in `markdown` |

**Errors**

| Status | When |
|---|---|
| `400` | The file could not be converted. The `detail` names the reason — `EncryptedError` (password-protected), `MalformedError` (structurally unusable), `UnsupportedError` (unknown format, or a scanned PDF that would need OCR), `ResourceLimitError` (a safety limit such as decompression was crossed) |
| `400` | The format could not be determined and no usable `format` was given |
| `413` | The upload is over `CONVERT_MAX_MB` |

An unreadable file is the caller's problem, not the server's, so it is a `400` and never a `500`.

**Two things worth knowing.** The bytes outrank the `format` field, exactly as on the crawl path:
the converter does what it is told, so honouring `format=csv` on a `.docx` would return a `200`
carrying mojibake — a wrong answer with no way for the caller to detect it. And this is the only
endpoint in the API that fetches nothing, so the SSRF guard has nothing to check here; the bytes
come from the client. `CONVERT_MAX_MB` bounds what reaches memory and the converter, but the
multipart body is already spooled to disk by the ASGI server before any of this runs — bound the
transfer itself at your reverse proxy (`client_max_body_size` and equivalents).

**Example:**

```bash
curl -X POST http://localhost:8000/v1/convert \
  -H "X-API-Key: <your key>" \
  -F "file=@rapport-annuel.xlsx"
```

```json
{
  "markdown": "# Rapport trimestriel\n\n|Region|T1|T2|\n|---|---|---|\n|EMEA|1200|1450|\n",
  "format": "xlsx",
  "title": "Rapport trimestriel",
  "word_count": 12
}
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

### POST /mcp

The [Model Context Protocol](https://modelcontextprotocol.io) surface, served over the
streamable-HTTP transport at `MCP_PATH` (default `/mcp`). Nine tools — `web_search`,
`fetch_url`, `map_site`, `crawl_site`, `extract_structured`, `answer`, `deep_research`,
`deep_research_estimate`, `job_status` — plus resources and prompts.

This is a **protocol endpoint, not a REST one**: the body is JSON-RPC and the session is
negotiated by the transport, so point an MCP client at the URL rather than hand-writing
requests. The same path also answers `GET` (the server→client event stream) and `DELETE`
(session termination); the client handles both. It is deliberately absent from
`/openapi.json` and `/docs` for the same reason: an OpenAPI operation describes a request
shape, and there is one JSON-RPC envelope here rather than nine. The tool schemas are
published over the protocol itself, by `tools/list`.

The server runs **in this process**, not as a sidecar calling the HTTP API back — one
browser pool, one search cache, one cost ledger, one job manager. A `crawl_site` or
`deep_research` tool call therefore returns an ordinary job id: the agent polls it with
`job_status`, and you can watch the very same job at [`GET /v1/jobs/{job_id}`](#get-v1jobsjob_id),
stream it at [`/stream`](#get-v1jobsjob_idstream) or cancel it with
[`DELETE /v1/jobs/{job_id}`](#delete-v1jobsjob_id).

**Auth required:** When `AUTH_ENABLED=true` — the same `API_KEYS`, resolved to the same
labels with the same constant-time comparison as `/v1/*`. Clients send the key as
`Authorization: Bearer <key>`, the header the MCP transport reads. With `AUTH_ENABLED=false`
the MCP surface is open, exactly like `/v1/*`.

```jsonc
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

Rate limiting is per **tool**, not per request — see [Rate limiting](#rate-limiting).
Every tool result is capped at `MCP_MAX_RESULT_TOKENS` (default `4000`) because a tool
result is re-sent on every following agent turn; the cut is marked in place and the full
payload stays readable as a `search-api://artifact/{id}` resource.

**Response `404`** when `MCP_ENABLED=false`:

```json
{
  "detail": "MCP server is not enabled on this instance."
}
```

The tool catalogue with every argument, the resource URIs, the prompts and the per-client
setup (including the stdio mode, `python -m app.mcp`) are documented in
[`MCP_REFERENCE.md`](MCP_REFERENCE.md).

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

### ConvertResponse

```json
{
  "markdown": "# Rapport trimestriel\n\n|Region|T1|T2|\n|---|---|---|\n|EMEA|1200|1450|\n",
  "format": "xlsx",
  "title": "Rapport trimestriel",
  "word_count": 12
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
| `CRAWL_ENABLE_DOCUMENTS` | `true` | Convert document URLs (Word, Excel, PowerPoint, OpenDocument, RTF, EPUB, CSV, PDF) to Markdown instead of rendering them |
| `CRAWL_DOC_MAX_MB` | `25` | Refuse documents larger than this (reported as a per-URL failure). Caps the **download**; decompression has its own limits inside the parser |
| `CRAWL_PDF_FALLBACK` | `true` | Fall back to `pypdf` when the converter fails on a PDF |
| `CONVERT_MAX_MB` | `25` | Max size of a file uploaded to `POST /v1/convert` |
| `CRAWL_ENABLE_PDF` | — | **Deprecated** alias for `CRAWL_ENABLE_DOCUMENTS`; honoured for one more version, with a warning at startup |
| `CRAWL_PDF_MAX_MB` | — | **Deprecated** alias for `CRAWL_DOC_MAX_MB`; same treatment |

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

### MCP server

The MCP surface is mounted in *this* process (see [`POST /mcp`](#post-mcp)), so it inherits
`CRAWL_MAX_CONCURRENCY`, the search cache, the cost ledger and the job manager from the
settings above. What follows is only what is specific to it.

| Variable | Default | Description |
|---|---|---|
| `MCP_ENABLED` | `true` | Serve the MCP surface. While false, `MCP_PATH` answers `404` like any unknown route |
| `MCP_PATH` | `/mcp` | Path the streamable-HTTP transport serves |
| `MCP_ALLOWED_HOSTS` | `""` | Comma-separated `Host` values the transport accepts (DNS-rebinding protection). Empty means **accept any Host**, not "refuse every one": in a container the request Host is the service name or your domain, never localhost, so the SDK's default allow-list would turn away every MCP request. Safe behind a reverse proxy that pins the Host; set it explicitly when the API is exposed directly |
| `MCP_MAX_RESULT_TOKENS` | `4000` | Token ceiling on **one** tool result. This is agent context, not bandwidth: whatever a tool returns is re-sent on every following turn. Truncation is from the top (the head of a page is what an agent needs to judge the rest), the cut is marked in place, and the remainder stays readable as a `search-api://artifact/{id}` resource. The budget is shared across the pages of a multi-URL call, never multiplied by them |
| `MCP_MAX_URLS_PER_CALL` | `10` | Max URLs a single `fetch_url` call may take. Lower than the HTTP cap on purpose: that one bounds the server's work, this one bounds an agent loop's |
| `MCP_ARTIFACT_TTL` | `1800` | Seconds a full tool payload stays readable as a resource |
| `MCP_ARTIFACT_MAX_ITEMS` | `256` | Max artefacts kept in memory (least-recently-used evicted) |
| `MCP_IMAGE_MODE` | `off` | `off`, `resource` or `inline`. Screenshots and PDF renders are never inlined by default: base64 in a tool result is context the agent pays for on every turn. `resource` returns a link the client may load; `inline` embeds the image |
| `MCP_RESEARCH_MAX_CONCURRENT` | `2` | Simultaneous `deep_research` runs per API-key label. `DAILY_COST_CAP_USD` is a finish-line limit; this one stops an agent loop from reaching it in thirty seconds |

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
