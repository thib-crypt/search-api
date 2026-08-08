# MCP Reference

The Unified Search API speaks the [Model Context Protocol](https://modelcontextprotocol.io)
as well as HTTP. Point an agent at `/mcp` and it gets nine tools, four resources and
two prompts — the same search, crawling, extraction and deep-research engine the REST
API exposes, shaped for something that has to *choose* between them.

- **Transport:** streamable HTTP at `MCP_PATH` (default `/mcp`), plus a stdio mode for
  desktop clients.
- **Auth:** `Authorization: Bearer <key>`, with the same `API_KEYS` as the HTTP API.
- **SDK:** `mcp>=2.0,<3` (protocol revision 2026-07-28).

---

## Table of Contents

- [Why it runs in the API process](#why-it-runs-in-the-api-process)
- [Connecting](#connecting)
  - [Claude Code](#claude-code)
  - [Claude Desktop](#claude-desktop)
  - [Any other client](#any-other-client)
  - [stdio (workstation mode)](#stdio-workstation-mode)
- [Tools](#tools)
  - [web_search](#web_search)
  - [map_site](#map_site)
  - [fetch_url](#fetch_url)
  - [crawl_site](#crawl_site)
  - [answer](#answer)
  - [extract_structured](#extract_structured)
  - [deep_research_estimate](#deep_research_estimate)
  - [deep_research](#deep_research)
  - [job_status](#job_status)
  - [What is deliberately not exposed](#what-is-deliberately-not-exposed)
- [The context budget](#the-context-budget)
- [Long-running work](#long-running-work)
- [Clarifying questions](#clarifying-questions)
- [Limits, rate limiting and cost](#limits-rate-limiting-and-cost)
- [Resources](#resources)
- [Prompts](#prompts)
- [Configuration](#configuration)
- [Observability](#observability)
- [Troubleshooting](#troubleshooting)

---

## Why it runs in the API process

The MCP server is mounted in the same ASGI application as the REST API, not run as a
sidecar that calls it back over HTTP. That is a deliberate choice, and it is the reason
the whole surface is cheap:

| | In-process (what this is) | Separate process |
|---|---|---|
| Browser pool | one, bounded by `CRAWL_MAX_CONCURRENCY` | two — the global cap stops capping anything |
| SearXNG cache | shared | duplicated, hit rate halved |
| Cost ledger | one file, one exact daily cap | two writers, or a cap that can be walked around |
| Jobs | an MCP job **is** an HTTP job | invisible from the other side |
| Latency | no hop | an HTTP round trip and a re-serialization per call |

The practical consequence worth remembering: **a job an agent starts is a job you can
inspect.** `crawl_site` and `deep_research` return an id that works at
`GET /v1/jobs/{id}`, streams over `GET /v1/jobs/{id}/stream`, and is cancelled by
`DELETE /v1/jobs/{id}`.

---

## Connecting

### Claude Code

```bash
claude mcp add --transport http search-api https://your-host/mcp --header "Authorization: Bearer $API_KEY"
```

Or in `.mcp.json`:

```json
{
  "mcpServers": {
    "search-api": {
      "type": "http",
      "url": "https://your-host/mcp",
      "headers": { "Authorization": "Bearer sk_your_key_here" }
    }
  }
}
```

### Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "search-api": {
      "type": "http",
      "url": "https://your-host/mcp",
      "headers": { "Authorization": "Bearer sk_your_key_here" }
    }
  }
}
```

### Any other client

Point it at `https://your-host/mcp` with an `Authorization: Bearer <key>` header. The
transport is streamable HTTP; the path is served exactly (no trailing slash, no
redirect), so `https://your-host/mcp` is the URL — not `https://your-host/mcp/`.

With `AUTH_ENABLED=false` the header is unnecessary and the surface is open, exactly as
`/v1/*` is. That is a local-development configuration; the app logs a warning at startup
if it sees it anywhere else.

### stdio (workstation mode)

```json
{
  "mcpServers": {
    "search-api": {
      "command": "python",
      "args": ["-m", "app.mcp"],
      "cwd": "/path/to/search-api",
      "env": { "SEARXNG_URL": "http://localhost:8080", "OPENAI_API_KEY": "sk-..." }
    }
  }
}
```

Reads the same `.env` as the HTTP app, so the model, the SearXNG URL and the crawl guards
are whatever the deployment already uses.

**This is a workstation mode, not a server mode.** The process is one client's: it starts
its own Chromium pool, its own SearXNG connection pool and its own LLM client, so
`CRAWL_MAX_CONCURRENCY` bounds *that process* and not the fleet. Run one next to a
deployed instance and you have two browser pools, not one shared budget. Rate limiting is
skipped for the same reason — there is no shared window to draw from over a pipe — while
`DAILY_COST_CAP_USD` still applies, because it is enforced on the ledger and a SQLite
ledger is shared.

---

## Tools

Nine tools, grouped by what they cost. The cost is written into every description, in
words, because the description is the only thing an agent reads before it chooses.

| Tool | Cost | Rate-limit points | Returns |
|---|---|---|---|
| `web_search` | free, instant | 1 | titles, URLs, snippets |
| `map_site` | free, one HTTP request | 2 | a site's URLs |
| `deep_research_estimate` | free, instant | 1 | a projected price |
| `job_status` | free | 1 | a job's status or result |
| `fetch_url` | seconds of browser time | 5 | page content as Markdown |
| `crawl_site` | minutes of browser time | 25 | a job id |
| `answer` | one LLM call | 10 | a sourced answer |
| `extract_structured` | one LLM call **per URL** | 10 | typed fields |
| `deep_research` | minutes and dollars | 100 | a job id |

### `web_search`

```
q: str
max_results: int = 8          (max 20)
time_range: "day"|"week"|"month"|"year" | null
include_domains / exclude_domains: string[] | null
language: str | null
```

Titles, URLs and snippets — **not** page content. The tool for finding out *which* pages
are worth reading; `fetch_url` reads them. Annotated `readOnlyHint`, `idempotentHint`,
`openWorldHint`.

### `map_site`

```
url: str                      (any URL on the site; its origin is the entry point)
search: str | null            (substring filter)
limit: int = 100              (max 300)
```

Reads `sitemap.xml`, then `robots.txt`, then failing both the links on the page you gave.
Run it **before** `crawl_site`: it tells you how big the site is, so you crawl the twenty
pages you want instead of the four hundred you don't.

### `fetch_url`

```
urls: string[]                (1..MCP_MAX_URLS_PER_CALL, default 10)
max_chars_per_page: int = 8000
include_links: bool = false
capture: "none"|"screenshot"|"pdf"    (only when MCP_IMAGE_MODE is not "off")
```

Clean Markdown, a few seconds per page, no model call. Handles PDFs, Word, Excel,
PowerPoint, OpenDocument, RTF, EPUB and CSV as well as HTML, via the same document
pipeline `POST /v1/crawl` uses.

The cleaning knobs — `content_filter`, `prune_threshold`, `word_count_threshold`,
`include_raw_markdown`, `exclude_external_links` — are **not** exposed. Nobody can set
them for a page they have not seen, and an agent asked for them will produce plausible
noise. They are instance settings, so they stay instance settings.

### `crawl_site`

```
url: str
max_pages: int = 25           (max 100)
max_depth: int = 2            (max 4)
url_patterns: string[] | null (globs, e.g. "*/docs/*")
```

**Minutes, not seconds.** Starts a job and returns its id; poll `job_status`. For a
handful of URLs you already know, `fetch_url` is far faster and returns immediately.

### `answer`

```
query: str
max_sources: int = 5          (max 10)
language: str | null
```

Search, read the top pages, one LLM call. A few seconds and a few cents, returned with
numbered sources. For a question whose answer fits in a paragraph or two.

`model` is **not** exposed. Letting an agent choose the model is letting an agent choose
the bill; the instance decides, through `LLM_MODEL`.

### `extract_structured`

```
urls: string[]                (1..5)
schema: object                (JSON Schema)
```

**One LLM call per URL.** If you only want the text of the pages, `fetch_url` is free.

The `css` mode of `POST /v1/extract` is not exposed: it needs a crawl4ai selector schema,
which nothing can write for a page it has not seen.

### `deep_research_estimate`

```
query: str
breadth: int = 4              (max 6)
depth: int = 2                (max 3)
```

Prices a `deep_research` run **without starting it**: pages it would read, model calls it
would make, projected dollars. Free and instant — no search, no crawl, no model call. The
figure is accurate to roughly a factor of two, which is enough to answer "cents or
dollars?".

A separate tool rather than an `estimate_only` flag on `deep_research`. A flag that
changes the *shape* of the response is exactly the kind of polymorphism an agent gets
wrong.

### `deep_research`

```
query: str
breadth: int = 4              (1..6)
depth: int = 2                (1..3)
answers: string[] | null      (answers to clarifying questions a previous call returned)
time_range / include_domains / exclude_domains
```

Recursive searches, dozens of pages read, a written report with numbered sources.
**Several minutes and several dollars per call.** Starts a job and returns its id, along
with the projected cost of the run it just launched.

It may first come back with clarifying questions — see
[Clarifying questions](#clarifying-questions).

### `job_status`

```
job_id: str
include_result: bool = true
```

While the job runs: its status and its last few steps, with how long to wait before
asking again. Once it finishes: the result, budgeted like any other tool result — a
400-page crawl does not arrive in one block.

### What is deliberately not exposed

| Left out | Why |
|---|---|
| `GET /v1/search` | same thing as the POST; an ETag means nothing over MCP |
| `formats: screenshot/pdf` inline | base64 in the transcript, paid for on every turn — see `MCP_IMAGE_MODE` |
| `model` on every tool | the agent would be choosing the bill |
| `mode: "css"` on extract | the selector schema is not guessable |
| `webhook_url` | an agent has no endpoint to be called back on; it is a server-to-server primitive |
| `stream: true` on answer | a tool result is delivered once, not typed out |
| `DELETE /v1/jobs/{id}` | cancellation is an operator action, not something to hand an agent |
| `/metrics`, `/health` | resources and operator endpoints, not tools |

---

## The context budget

**No tool result exceeds `MCP_MAX_RESULT_TOKENS` (default 4000).**

This is the difference between "I wrapped my API" and a usable MCP server. An HTTP
response is read once and dropped. A tool result is not: it lands in the agent's
transcript and is re-sent, in full, on every following turn. Ten pages of documentation
returned verbatim is 400 kB of Markdown the agent pays for over and over — crowding out
the conversation that made the call worth anything.

So every tool:

1. produces its complete payload, unchanged, through the same services the REST API uses;
2. returns the metadata in full as `structuredContent` — URLs, statuses, counters, usage:
   small, and the part an agent reasons over rather than reads;
3. returns the text truncated, with the cut marked in place:
   `[truncated: 8,000 of 47,231 characters. Read the rest at search-api://artifact/a1b2c3/page/2]`;
4. attaches one `resource_link` — **only when something was actually cut.** A result that
   fit gets no link: a pointer to text the agent already has is one more thing to read for
   nothing.

Two details matter more than they look.

**Truncation is from the top, never the middle.** The head of a page carries the title,
the intro and the headings — exactly what an agent needs to decide whether the rest is
worth fetching. Keeping the "interesting" middle would take that decision away.

**The budget is global, then divided.** Ten URLs share one budget rather than getting ten.
A URL that failed does not hold a share — it is redistributed over the ones that produced
text, so asking for ten pages and getting two back returns two useful pages, not two thin
ones.

### Screenshots and PDF renders

Never inline by default. `MCP_IMAGE_MODE`:

- `off` (default) — the `capture` argument is not offered at all;
- `resource` — the image is stored as an artefact and returned as a `resource_link` the
  client loads if it wants to;
- `inline` — an `ImageContent` block, for vision-capable clients, enabled knowingly.

---

## Long-running work

`crawl_site` and `deep_research` take minutes. A synchronous tool call that blocks for
eight of them is a call most clients time out, so neither returns its result:

```
crawl_site(url=...)
  → { "job_id": "a1b2...", "status": "pending",
      "poll_with": { "tool": "job_status", "arguments": {"job_id": "a1b2..."},
                     "wait_seconds_between_polls": 10 },
      "resource": "search-api://job/a1b2...",
      "http_status_url": "/v1/jobs/a1b2...", "http_stream_url": "/v1/jobs/a1b2.../stream" }
```

No job engine was added for this. `JobManager` already does creation, background
execution, progress, terminal status, cancellation, SQLite persistence and restart
reconciliation; the MCP layer only translates. Suggested poll intervals: 20 s for
research, 10 s for site crawls.

> **A note on MCP tasks.** The `tasks/*` primitive of the 2025-11-25 revision would have
> been the natural fit here, and the plan for this phase targeted it. The revision this
> SDK speaks — 2026-07-28 — removed it: `mcp.types` still defines `Task` and
> `CreateTaskResult`, but `tasks/get` is not in the SDK's method table and there is no
> server-side handler for it. Serving them would mean hand-rolling protocol handlers for a
> revision the runtime has moved past, which no client would look for anyway. So the
> polling path — which the plan already specified in full as the mandatory fallback — is
> the only path. One code path, no capability negotiation, and a job that is identical to
> one the HTTP API started.

---

## Clarifying questions

A broad `deep_research` query can be worth pinning down before spending dollars on it.
Three paths, and all three end with the run going ahead:

- **Client supports `elicitation`** — the questions become a form the client puts in front
  of the user, and the answers fold into the *same* tool call. The agent sees a
  `deep_research` that worked, not a call it has to understand and retry.
- **Client does not** — the questions come back as the tool result, with an instruction to
  call `deep_research` again with an `answers` array. This is the HTTP behaviour.
- **The user declines or cancels the form** — the run starts unclarified. Declining to
  elaborate is not declining the research.

A query that is already specific skips all of this: the heuristic that guards
`POST /v1/research` guards this too, and costs nothing when it fires.

---

## Limits, rate limiting and cost

Every ceiling here is **lower** than its HTTP equivalent — 10 URLs instead of 50, breadth
6 instead of 10. The HTTP caps bound a server's work; these bound an autonomous loop's
spending.

**Rate limiting.** The same weighted sliding window as the HTTP API, keyed on the same
API-key label, drawing from the same budget. There is a wrinkle worth knowing about:
`RateLimitMiddleware` prices a request from its *path*, because it runs before routing —
and every tool shares the path `/mcp`. It charges 1 point on the way in; the MCP layer
debits the remainder once it knows which tool was asked for. A `deep_research` therefore
costs 100 points, not 1.

A refused call comes back as a tool result the model can read, stating the cost, the
remaining budget and the reset delay — an agent can act on that, where an opaque 429 only
gets retried into the same wall.

**Daily cost cap.** `DAILY_COST_CAP_USD` applies to `answer`, `extract_structured` and
`deep_research`, checked before any work starts and billed to the same per-label ledger as
the HTTP endpoints. Over the cap, the free tools keep working.

**Concurrency.** `MCP_RESEARCH_MAX_CONCURRENT` (default 2) bounds simultaneous
`deep_research` runs per key. This one is specific to agents: a person runs one research
report and waits for it, where a loop can start twenty in a minute and reach the daily cap
before anyone notices. The daily cap is a finish-line limit; this is the guard rail before
it.

**SSRF.** Unchanged, and unchanged deliberately. Every URL an MCP tool crawls goes through
the same `UrlGuard` as any other, because the tools call `CrawlerService` rather than
reimplementing it.

---

## Resources

| URI | Contents |
|---|---|
| `search-api://artifact/{id}` | the complete, untruncated payload of one tool call |
| `search-api://artifact/{id}/page/{n}` | one page of a multi-URL result, in full |
| `search-api://job/{id}` | a job's status and, once finished, its result |
| `search-api://capabilities` | the limits this instance actually enforces |

`search-api://capabilities` is the one worth reading first. It publishes the real
ceilings — URLs per call, token budget, breadth and depth maxima, the per-tool rate-limit
costs, and how much of the daily budget this key has left. An agent that can read the
constraints stops guessing them, and stops retrying calls the server will always refuse.

Artefacts live in memory, bounded by `MCP_ARTIFACT_TTL` and `MCP_ARTIFACT_MAX_ITEMS`, and
do not survive a restart. That is the right trade: an artefact is expensive to *transport*,
not to recompute — the crawl that produced it is minutes old and the page is still there.

---

## Prompts

Two, and only because they change what a run costs rather than merely wording it more
nicely.

- **`research_brief(topic)`** — turns a loose ask into a research question
  `deep_research` can answer, with a breadth/depth suggestion, domains worth excluding, and
  an instruction to price it first. The difference between a $4 report on the wrong topic
  and a $4 report on the right one.
- **`verify_claim(claim)`** — checks a statement against sources using `web_search` and
  `fetch_url`, reading the sources rather than asking a model to vouch for them.

---

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `MCP_ENABLED` | `true` | Serve the MCP endpoint at all. `false` makes `MCP_PATH` a 404. |
| `MCP_PATH` | `/mcp` | Path the streamable-HTTP transport serves. |
| `MCP_ALLOWED_HOSTS` | *(empty)* | Comma-separated `Host` values accepted (DNS-rebinding protection). Empty means any Host — see below. |
| `MCP_MAX_RESULT_TOKENS` | `4000` | Token ceiling on one tool result. |
| `MCP_MAX_URLS_PER_CALL` | `10` | Max URLs in one `fetch_url` call. |
| `MCP_ARTIFACT_TTL` | `1800` | Seconds a truncated payload stays readable. |
| `MCP_ARTIFACT_MAX_ITEMS` | `256` | Artefacts kept in memory (LRU eviction). |
| `MCP_IMAGE_MODE` | `off` | `off` \| `resource` \| `inline`. |
| `MCP_RESEARCH_MAX_CONCURRENT` | `2` | Simultaneous `deep_research` runs per key. |

**`MCP_ALLOWED_HOSTS` deserves a sentence.** The MCP SDK turns DNS-rebinding protection on
automatically when a server binds localhost, with an allow-list of localhost hosts. In a
container the request `Host` is the service name or your public domain, so that default
would refuse **every** MCP request — a failure that looks like a broken transport rather
than a misconfiguration. The rule here is therefore explicit: empty means the check is
off, which is correct behind a reverse proxy that pins the `Host` itself. Set it when the
API is exposed directly.

Browser-based MCP clients also need to read the session id the transport issues;
`app/main.py` exposes `Mcp-Session-Id` through CORS for exactly that.

---

## Observability

`GET /metrics` gains four series alongside the existing ones:

| Metric | Labels | Why |
|---|---|---|
| `mcp_tool_calls_total` | `tool`, `label`, `outcome` | `outcome` is `success` \| `error` \| `refused` — "refused" being a call a guard turned away *before* it spent anything, which is the series worth alerting on |
| `mcp_tool_duration_seconds` | `tool` | latency per tool |
| `mcp_result_tokens` | `tool` | the check that `MCP_MAX_RESULT_TOKENS` survives contact with real pages |
| `mcp_artifacts_stored` | — | how much truncated payload is being held |

OpenTelemetry traces follow automatically: `/mcp` is a route of the same instrumented app.

---

## Troubleshooting

**Every request comes back 401.** The key must be sent as `Authorization: Bearer <key>`.
The `X-API-Key` header the REST API also accepts is not part of MCP's auth model and is
not read here.

**Every request is refused with an invalid-Host error.** `MCP_ALLOWED_HOSTS` is set and
does not list the `Host` your clients send. Add it, or clear the variable if a reverse
proxy already pins the `Host`.

**A client gets a redirect before every call.** It is configured with a trailing slash.
The path is served exactly; use `https://host/mcp`.

**`deep_research` keeps returning questions instead of a report.** The client does not
support elicitation, so it is falling back to the documented path: call it again with an
`answers` array, one entry per question, in order. `answers: ["as stated"]` researches the
query as it is.

**A browser-based client cannot hold a session.** It needs to read `Mcp-Session-Id`, which
means the CORS layer must expose it — it does, but check that a proxy in front is not
stripping the header.
