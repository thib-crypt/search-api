# API Reference

Complete reference for the Unified Search API v0.1.0.

Interactive docs are also served at `/docs` (Swagger UI) and `/redoc` when the server is running.

---

## Table of Contents

- [Base URL & versioning](#base-url--versioning)
- [Authentication](#authentication)
- [Request IDs](#request-ids)
- [Rate limiting](#rate-limiting)
- [Error format](#error-format)
- [Endpoints](#endpoints)
  - [GET /](#get-)
  - [GET /health](#get-health)
  - [POST /v1/search](#post-v1search)
  - [POST /v1/crawl](#post-v1crawl)
  - [POST /v1/crawl/site](#post-v1crawlsite)
  - [POST /v1/answer](#post-v1answer)
  - [POST /v1/research](#post-v1research)
  - [GET /v1/jobs/{job\_id}](#get-v1jobsjob_id)
  - [GET /v1/jobs/{job\_id}/stream](#get-v1jobsjob_idstream)
- [Schemas](#schemas)
- [Configuration reference](#configuration-reference)
- [SSE streaming format](#sse-streaming-format)

---

## Base URL & versioning

```
http://<host>:8000
```

All resource endpoints are prefixed with `/v1`. Utility endpoints (`/health`, `/`) have no prefix.

---

## Authentication

Authentication is **optional** and controlled by `AUTH_ENABLED` (default: `false`).

When enabled, every `/v1/*` endpoint requires a valid API key. Send it in either header — the first one found is used:

| Header | Format |
|---|---|
| `Authorization` | `Bearer <key>` |
| `X-API-Key` | `<key>` |

Valid keys are set via the `API_KEYS` environment variable (comma-separated list).

**401 response when auth fails:**

```json
{
  "detail": "Invalid or missing API key"
}
```

> `/health`, `/docs`, `/openapi.json`, and `/redoc` are always public.

---

## Request IDs

Every response includes an `X-Request-ID` header. The server either mirrors the value you send in the request header, or generates a UUID automatically. Use this ID when reporting bugs or correlating logs.

---

## Rate limiting

Controlled by `RATE_LIMIT_ENABLED` (default: `false`) and `RATE_LIMIT_RPM` (default: `120`).

When enabled, requests are counted per client IP over a sliding 60-second window. Exceeding the limit returns:

```
HTTP 429 Too Many Requests
Retry-After: <seconds>
```

Exempt paths: `/health`, `/docs`, `/openapi.json`, `/redoc`.

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
| `400` | Bad request / validation error |
| `401` | Missing or invalid API key |
| `404` | Resource not found (e.g. unknown job ID) |
| `429` | Rate limit exceeded |
| `500` | Unexpected server error |
| `502` | Upstream failure (SearXNG unreachable, crawl error, LLM error) |
| `503` | Service not yet initialised |

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
  "endpoints": { ... }
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

### POST /v1/crawl

Crawls one or more URLs and returns their content as clean Markdown. Requests are executed concurrently (up to `CRAWL_MAX_CONCURRENCY`).

**Auth required:** When `AUTH_ENABLED=true`

**Request body** (`application/json`):

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `urls` | `string[]` | Yes | — | URLs to crawl (1–50, must be valid HTTP/HTTPS URLs) |
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
| `count` | `integer` | Number of successfully crawled pages |
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
| `internal_links` | `string[]` | Internal links found on the page |
| `external_links` | `string[]` | External links found on the page |
| `error` | `string \| null` | Error message if `success=false` |

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
| `status` | `"pending"` | Initial job status |

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
  "status": "pending"
}
```

When the job completes, `result` in `/v1/jobs/{id}` contains the same structure as `CrawlResponse`.

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

**Response `200`** — `AnswerResponse`:

| Field | Type | Description |
|---|---|---|
| `query` | `string` | Original question |
| `answer` | `string` | LLM-synthesised answer with `[1]`, `[2]` … inline citations |
| `model` | `string` | LLM model that generated the answer |
| `sources` | `Source[]` | Ordered list of consulted sources |

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
| `report` | `string` | Full Markdown research report (typically 3+ pages) |
| `learnings` | `string[]` | Key facts extracted during research |
| `sources` | `string[]` | Deduplicated list of source URLs consulted |
| `serp_queries` | `string[]` | All search queries generated during the run |

#### 3. `JobSubmitted` (when `background: true`)

```json
{
  "job_id": "a3f8b2c1d4e5f6a7b8c9d0e1f2a3b4c5",
  "status": "pending"
}
```

Track with `/v1/jobs/{id}`. The final `result` mirrors the `completed` shape above.

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
es.onmessage = (e) => {
  const event = JSON.parse(e.data);
  if (event.event === 'completed') es.close();
};
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
  "max_results": null
}
```

### CrawlRequest

```json
{
  "urls": ["https://example.com"],
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

### AnswerRequest

```json
{
  "query": "string",
  "max_sources": 5,
  "model": null,
  "language": null,
  "categories": null,
  "fetch_full": true
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
  "background": false
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

### Authentication

| Variable | Default | Description |
|---|---|---|
| `AUTH_ENABLED` | `false` | Require API key on all `/v1/*` routes |
| `API_KEYS` | `""` | Comma-separated list of valid API keys |

### Hardening

| Variable | Default | Description |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `false` | Enable per-IP rate limiting |
| `RATE_LIMIT_RPM` | `120` | Max requests per minute per IP |
| `SECURITY_HEADERS` | `true` | Add `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` headers |
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed CORS origins |

### SearXNG

| Variable | Default | Description |
|---|---|---|
| `SEARXNG_URL` | `http://searxng:8080` | SearXNG instance base URL |
| `SEARXNG_TIMEOUT` | `15.0` | Request timeout in seconds |

### LLM

| Variable | Default | Description |
|---|---|---|
| `LLM_MODEL` | `openai/gpt-4o-mini` | Default model (litellm provider string) |
| `LLM_REPORT_MODEL` | `""` | Optional dedicated model for research reports (falls back to `LLM_MODEL`) |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature |
| `LLM_MAX_TOKENS` | `4096` | Maximum output tokens |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `OPENROUTER_API_KEY` | — | OpenRouter API key |

Any other provider API key recognised by [litellm](https://docs.litellm.ai/docs/providers) can be added.

### Crawling

| Variable | Default | Description |
|---|---|---|
| `CRAWL_MAX_CONCURRENCY` | `5` | Max concurrent browser crawl tasks |
| `CRAWL_TIMEOUT` | `30.0` | Per-page crawl timeout in seconds |

### Research

| Variable | Default | Description |
|---|---|---|
| `RESEARCH_DEFAULT_BREADTH` | `4` | Default SERP queries per recursion level |
| `RESEARCH_DEFAULT_DEPTH` | `2` | Default recursion depth |
| `RESEARCH_CONCURRENCY` | `2` | Parallel query workers during research |

---

## SSE streaming format

The `/v1/jobs/{id}/stream` endpoint uses Server-Sent Events. Each event is a JSON object delivered as `data: <json>\n\n`.

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
  "current_depth": 1,
  "total_depth": 2,
  "completed_queries": 3,
  "total_queries": 8,
  "message": "searching: quantum error correction"
}
```

```json
{
  "event": "progress",
  "stage": "processed",
  "current_depth": 1,
  "total_depth": 2,
  "completed_queries": 4,
  "total_queries": 8,
  "message": "processed: quantum error correction"
}
```

**For site crawl jobs:**

```json
{
  "event": "progress",
  "pages_crawled": 12,
  "message": "crawled: https://docs.example.com/guide"
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
data: {"event": "status", "status": "running"}

data: {"event": "progress", "stage": "searching", "current_depth": 1, "total_depth": 2, "completed_queries": 0, "total_queries": 4, "message": "searching: AI coding tools 2025"}

data: {"event": "progress", "stage": "processed", "current_depth": 1, "total_depth": 2, "completed_queries": 1, "total_queries": 4, "message": "processed: AI coding tools 2025"}

data: {"event": "completed"}

data: {"event": "__end__"}
```
