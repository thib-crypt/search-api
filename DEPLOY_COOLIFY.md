# Deploying on Coolify

Target: the API (`app.main:app`, port 8000) plus its bundled SearXNG, deployed as a
**Docker Compose build pack** on a Coolify v4 server.

The repo ships [`docker-compose.coolify.yml`](docker-compose.coolify.yml) for this. Point
Coolify at that file, not at `docker-compose.yml` — the latter targets a local machine and
will not work here (see [Why two compose files](#why-two-compose-files)).

---

## 1. Server requirements

| Resource | Minimum | Comfortable | Why |
|---|---|---|---|
| RAM | 4 GB | 8 GB | `CRAWL_MAX_CONCURRENCY=5` means up to 5 concurrent Chromium instances (~250–400 MB each), plus SearXNG and the API. |
| CPU | 2 vCPU | 4 vCPU | Page rendering is CPU-bound. |
| Disk | 10 GB | 20 GB | The `unclecode/crawl4ai` base image (Playwright + Chromium) is ~2–3 GB, before build layers. |

The **first build takes 10–20 minutes** (base image pull + `playwright install chromium`).
If Coolify times out, raise the build timeout in *Server → Advanced* or in the
application's settings. Keep the build cache enabled: the `Dockerfile` pins its base image
and `uv` by digest, so an uncached rebuild starts from zero every time.

---

## 2. Create the application

1. *Project → + New Resource → **Docker Compose*** (source: Git — public repo, or private
   via Coolify's GitHub app).
2. Branch: `main`.
3. **Docker Compose Location**: `/docker-compose.coolify.yml`.
4. Base Directory: `/`.
5. Do **not** tick "instant deploy" — the environment variables come first (step 3).

### Domain and routing

*Configuration → General* shows a domain field **per compose service**. Set a domain on
the `api` service only, and include the port, since the container does not listen on 80:

```
https://search.example.com:8000
```

**The scheme decides the TLS.** Coolify only requests a Let's Encrypt certificate for a
domain declared `https://`. Write `http://` and the service is served in cleartext on port
80 while 443 answers `503` behind Traefik's default self-signed certificate. The port is
the *container's* port: Coolify strips it from the proxy rule, so the service ends up
served at `https://search.example.com`.

Leave `searxng` **without a domain**. It is an unauthenticated search backend; exposing it
publicly hands an SSRF payload a ready-made target. Do not fill in *Ports Mappings* for
either service.

### Health check

*Configuration → Healthcheck*: path `/health`, port `8000`.

`/health` is the liveness probe and deliberately depends on nothing else. `/health/ready`
is richer — it returns 503 while SearXNG is unreachable or no provider key is set — but do
not wire it as the container healthcheck: a provider outage would put the container into a
restart loop.

---

## 3. Environment variables

*Configuration → Environment Variables*. Every `${VAR}` in the compose file appears here as
an editable field. All are **runtime** variables (none is needed at build time); tick
"Is Secret" on the keys.

### Required

The compose file marks these `${VAR:?}`, so a deploy with any of them missing fails with a
named error instead of starting up broken.

```
API_KEYS=mobile-app:<key>,my-script:<key>
LLM_MODEL=openrouter/anthropic/claude-sonnet-4.5
SEARXNG_SECRET=<64 hex>
```

...plus **one** provider key matching the `LLM_MODEL` prefix — `OPENROUTER_API_KEY`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `GEMINI_API_KEY`. Set only the one you use: an
unused key in the environment is one more key that can leak.

Generate secrets locally:

```bash
openssl rand -hex 32
```

`API_KEYS` takes `label:key` entries. Give every client its own label — logs, rate-limit
buckets and cost counters are all keyed on the **label**, never on the key, so revoking a
client is a one-line edit plus a redeploy. The app **refuses to start** with
`AUTH_ENABLED=true` and an empty `API_KEYS`; if the container crash-loops right after the
first deploy, check that first in the logs.

### Recommended

```
LLM_REPORT_MODEL=openrouter/anthropic/claude-sonnet-4.5
RESEARCH_RERANK_MODEL=openrouter/google/gemini-2.5-flash
LLM_FALLBACK_MODELS=openrouter/openai/gpt-4o-mini
DAILY_COST_CAP_USD=5
RESEARCH_MAX_COST_USD=1
CORS_ORIGINS=https://my-frontend.example.com
WEBHOOK_SECRET=<64 hex>          # only if clients pass webhook_url
```

Anything not listed in the compose file keeps its default from
[`app/config.py`](app/config.py) (mirrored in [`.env.example`](.env.example)). Add a line to
the compose file to make another setting steerable from the UI.

---

## 4. Model and cost setup

Three settings, three cost profiles — this is where a `/v1/research` bill is decided:

| Variable | Used for | Pick |
|---|---|---|
| `LLM_MODEL` | SERP queries, learning extraction, `/v1/answer` | fast and cheap |
| `LLM_REPORT_MODEL` | the final report (`LLM_REPORT_MAX_TOKENS=8192`) | the most capable |
| `RESEARCH_RERANK_MODEL` | pre-crawl relevance filter, one call per SERP | the cheapest available |

`RESEARCH_RERANK=llm` (the default) calls the reranker on every result page. Put an
expensive model there and reranking costs more than the crawls it saves — use a "flash"
tier, or switch to `RESEARCH_RERANK=lexical` for no LLM at all.

`LLM_FALLBACK_MODELS` is tried in order once the primary model has exhausted
`LLM_MAX_RETRIES`.

**Two cost guards, not one.** `total_cost_usd` comes from litellm's price table, which does
not know every model slug — an unknown slug is billed as **$0**, so `DAILY_COST_CAP_USD`
can let it run indefinitely. The application cap is a safety net, not an invoice. Set the
real limit at the provider (for OpenRouter: *Settings → Keys → credit limit* on the key
this deployment uses). Note also that `DAILY_COST_CAP_USD` only survives a restart with
`JOB_BACKEND=sqlite` and the named volume — both of which the compose file sets.

### OpenRouter specifics

litellm routes on the prefix, so an OpenRouter model is its OpenRouter slug with
`openrouter/` prepended. Check the exact slug at <https://openrouter.ai/models> before
deploying: a wrong slug only shows up on the first LLM call (a 4xx from the provider), not
at startup.

```
openrouter/anthropic/claude-sonnet-4.5
openrouter/openai/gpt-4o-mini
openrouter/google/gemini-2.5-flash
openrouter/meta-llama/llama-3.3-70b-instruct
```

`OR_APP_NAME` and `OR_SITE_URL` are optional; litellm forwards them as `X-Title` and
`HTTP-Referer` for attribution in the OpenRouter dashboard.

---

## 5. Verify the deployment

```bash
curl -s https://search.example.com/health
```

```bash
curl -s https://search.example.com/health/ready | jq
```

Both probes are public — the health router carries no `require_api_key`, unlike `/metrics`
and every `/v1/*` route. Handy for an uptime monitor; worth knowing that the readiness body
names your provider.

Expected: `"status": "ready"` with three checks — `searxng: reachable`,
`crawler: browser pool not started yet (lazy)` (normal before the first crawl) and
`llm: provider '<name>' key configured`.

Then one real end-to-end call (search → crawl → LLM):

```bash
curl -s -X POST https://search.example.com/v1/answer -H "Authorization: Bearer <your-key>" -H "Content-Type: application/json" -d '{"query":"what is Coolify","max_sources":3}' | jq
```

The first request that triggers a crawl pays for Chromium's cold start (a few seconds).
That is not a symptom.

For long research runs, use `background=true` and follow the job over SSE rather than
holding an HTTP request open for minutes behind the proxy:

```bash
curl -s -X POST https://search.example.com/v1/research -H "Authorization: Bearer <your-key>" -H "Content-Type: application/json" -d '{"query":"...","background":true}' | jq
```

---

## Why two compose files

[`docker-compose.yml`](docker-compose.yml) is the local-machine setup, and each of its three
deployment-specific choices breaks on Coolify:

| `docker-compose.yml` | Problem under Coolify | What `docker-compose.coolify.yml` does |
|---|---|---|
| `ports: "${API_PORT}:8000"` | Bypasses the proxy, and loses rolling updates | `expose: 8000`, proxy owns the routing |
| `env_file: .env` | `.env` is gitignored and absent from the cloned repo | `${VAR}` interpolation, values from the UI |
| `volumes: ./data:/srv/api/data` | Bind mount is created **root** on the host; the container runs as `appuser`, so SQLite cannot write `jobs.db` | named volume `api-data`, which inherits the image's ownership |
| `volumes: ./searxng:/etc/searxng` | Coolify turns the bind mount into a managed storage pointing at an **empty** directory it creates; `settings.yml` never reaches the container, SearXNG boots without the `json` format and answers **403** to every query (the API surfaces it as a 502) | `searxng/Dockerfile` copies `settings.yml` into the image — no bind mount at all |

---

## Known pitfalls

- **`TRUSTED_PROXY=true` is right here and only here.** The middleware reads the first
  entry of `X-Forwarded-For` ([`app/api/middleware.py`](app/api/middleware.py)). Behind
  Coolify's proxy that is the real client IP; on a directly published port, a client could
  forge the header and slip past the rate limiter.
- **`CRAWL_ALLOW_PRIVATE_NETWORKS` stays `false`.** Set to `true`, any client can make the
  server read `169.254.169.254`, the SearXNG container, or anything else on your Coolify
  host's internal network.
- **`SEARXNG_SECRET`.** [`searxng/settings.yml`](searxng/settings.yml) carries SearXNG's
  own `ultrasecretkey` placeholder, overridden at startup by the environment variable. The
  instance is not exposed (no domain, no published port) so the impact is low, but set the
  variable anyway.
- **Never bind-mount a config directory here.** `settings.yml` is baked into the SearXNG
  image on purpose (see [Why two compose files](#why-two-compose-files)). Reintroduce
  `volumes: ./searxng:/etc/searxng` and Coolify silently mounts an empty directory over it,
  which reads as "SearXNG returned 403" at the first search — hours from the edit that
  caused it.
- **`/metrics` is authenticated** (it publishes spend per label) and exempt from rate
  limiting. Give an external Prometheus its own label in `API_KEYS`, e.g.
  `prometheus:<key>`.
- **Redeploy after changing a variable.** Runtime variables are read once at container
  start: *Restart* or *Redeploy* after every edit in the UI.
- **The MCP surface is live too.** `MCP_ENABLED` defaults to true, so `/mcp` is served from
  the same process and authenticates with the same `API_KEYS`. See
  [`MCP_REFERENCE.md`](MCP_REFERENCE.md).

---

## Updating

Push to `main` → Coolify redeploys (with auto-deploy on), or click *Redeploy*. The
`api-data` volume survives: finished jobs, their results and the daily cost ledger are
kept. If a deploy goes wrong, *Deployments → rollback* to the previous one.
