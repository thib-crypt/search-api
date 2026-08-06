# Load testing

`locustfile.py` drives `/v1/search` and `/v1/answer` against a **real, running
deployment**. It is not part of CI and never will be: a meaningful number needs a
real SearXNG instance, a real browser pool and a real LLM provider, and a GitHub
Actions runner has none of them.

For the application's own overhead — routing, middleware, validation,
serialization, with the network faked out — use [`scripts/bench.py`](../scripts/bench.py)
instead. It runs in-process, takes seconds, and *is* wired into CI as an
informational step. The two tools answer different questions:

| | `scripts/bench.py` | `loadtest/locustfile.py` |
|---|---|---|
| Measures | the API's own overhead | the whole system under concurrency |
| Dependencies | faked | real |
| Runs in CI | yes (informational) | no |
| Costs money | no | yes — `/v1/answer` spends provider credits |

## Running it

```bash
uv sync --extra loadtest

# Web UI on http://localhost:8089, then set users/spawn rate there
LOCUST_API_KEY=sk-your-key uv run locust \
  -f loadtest/locustfile.py --host https://api.example.com

# Headless, with the ramp fixed up front
LOCUST_API_KEY=sk-your-key uv run locust \
  -f loadtest/locustfile.py --host https://api.example.com \
  --headless --users 50 --spawn-rate 5 --run-time 10m \
  --csv results/run-1
```

`LOCUST_API_KEY` is sent as `X-API-Key`. Against an instance with
`AUTH_ENABLED=true` (the production default), omitting it means every request
401s and the run measures nothing — locust prints a warning at start.

Use `--tags search` to exclude the expensive endpoint entirely:

```bash
uv run locust -f loadtest/locustfile.py --host ... --tags search --headless \
  --users 100 --spawn-rate 10 --run-time 5m
```

## Load shape

The user classes are weighted 9:1 (search:answer), which roughly matches real
traffic and, more importantly, respects the weighted rate limiter: `/v1/answer`
costs 10 points against `/v1/search`'s 1, so a flat mix hits the limiter long
before it hits the service.

A reasonable first run:

| Phase | Users | Spawn rate | Duration | Purpose |
|---|---|---|---|---|
| Baseline | 5 | 1/s | 2 min | Uncontended latency — the floor to compare against |
| Ramp | 5 → 50 | 5/s | 5 min | Where does p95 start to bend? |
| Sustain | 50 | — | 10 min | Steady state; look for drift, not just a peak |
| Spike | 50 → 150 | 25/s | 2 min | Recovery behavior after the limiter kicks in |

Raise `--users` past 150 only after checking `RATE_LIMIT_RPM` on the target —
past that point you are measuring the limiter.

## What to look for

- **p50 vs p95 on `/v1/search`.** p50 tracks the happy path; a p95 several times
  p50 means queueing. Compare a cold run against a warm one: the search cache
  (300 s TTL) plus single-flight should make repeated queries much cheaper, and
  a warm p50 that has *not* improved suggests the cache is not being hit.
- **p95 on `/health` while the rest is saturated.** This is the loop-blocking
  canary. `/health` depends on nothing and is exempt from rate limiting, so it
  should stay flat and fast no matter what else is running. If it climbs,
  something is blocking the single worker's event loop — the failure mode §11 of
  `IMPROVEMENT_PLAN.md` calls critical for this deployment shape.
- **Error rate, split by status.** 429s are the limiter working as designed and
  should not be read as failures (the locustfile marks them as successes for
  `/v1/answer`). 502s and 504s are real: they mean SearXNG, the crawler or the
  provider gave up.
- **`/metrics` on the target, during the run.** `crawler_browser_sessions_active`
  must stay at or below `CRAWL_MAX_CONCURRENCY`; if it exceeds it, the global
  semaphore has a hole. Watch the request histogram there too, as a cross-check
  on locust's own client-side timings.
- **Bytes transferred.** Compression is on for JSON and markdown, so `/v1/crawl`
  responses should be several times smaller than their uncompressed size. Locust
  reports the compressed figure.
