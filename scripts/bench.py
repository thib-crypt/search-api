#!/usr/bin/env python
"""In-process latency benchmark for the app's own overhead (§8).

**What this measures.** Routing, the ASGI middleware stack (request id, security
headers, weighted rate limiting, gzip), Pydantic validation and JSON
serialization -- with SearXNG, crawl4ai and the LLM replaced by immediate fakes.
What is left is the cost the API adds on top of its dependencies, which is the
part a code change can regress.

**What this does not measure.** Anything real: no network, no browser, no
provider. A `/v1/answer` here returns in microseconds because nothing is
actually searched or generated. For end-to-end numbers against a live
deployment, use the locust suite in `loadtest/` instead -- the two answer
different questions and neither replaces the other.

Runs in CI as an informational step: p50/p95 on a shared runner are far too
noisy to gate on, but a step change across PRs is visible in the job log.

    uv run python scripts/bench.py                 # default 300 requests/endpoint
    uv run python scripts/bench.py --requests 2000
    uv run python scripts/bench.py --json          # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# Same environment the test suite sets up, for the same reasons: no auth, no
# rate limiting, no disk. Must happen before `app.main` is imported.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ["AUTH_ENABLED"] = "false"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["API_KEYS"] = ""
os.environ["JOB_BACKEND"] = "memory"
os.environ["CRAWL_PER_HOST_DELAY"] = "0"
os.environ["CRAWL_ALLOW_PRIVATE_NETWORKS"] = "true"
# One access-log line per request would bury the results and, worse, put a JSON
# encode and a write on the critical path of the thing being measured.
os.environ["LOG_LEVEL"] = "WARNING"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient  # noqa: E402

from app.api.deps import get_crawler, get_llm, get_searxng  # noqa: E402
from app.main import app  # noqa: E402
from app.models.crawl import CrawledPage  # noqa: E402
from app.models.search import SearchResponse, SearchResultItem  # noqa: E402


class FakeSearxng:
    """Returns instantly. The point is to take the network out of the measurement."""

    async def search(self, req: Any) -> SearchResponse:
        items = [
            SearchResultItem(
                title=f"Result {i}",
                url=f"https://example.com/{i}",
                snippet="A snippet of roughly the length a real SERP returns. " * 3,
            )
            for i in range(req.max_results or 10)
        ]
        return SearchResponse(query=req.q, number_of_results=len(items), results=items)


class FakeCrawler:
    async def crawl(self, req: Any) -> list[CrawledPage]:
        return [
            CrawledPage(
                url=str(u),
                success=True,
                status_code=200,
                markdown="# Page\n\nParagraph of body text. " * 40,
                word_count=200,
            )
            for u in req.urls
        ]


class FakeLLM:
    default_model = "bench/model"

    async def complete(self, messages: Any, **kwargs: Any) -> str:
        return "A synthesized answer citing [1] and [2]. " * 10


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; `statistics.quantiles` needs n > 1 and interpolates."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(pct / 100.0 * len(ordered) + 0.5)) - 1, len(ordered) - 1)
    return ordered[max(index, 0)]


def _measure(
    client: TestClient, name: str, method: str, path: str, payload: dict[str, Any] | None, n: int
) -> dict[str, Any]:
    call = client.get if method == "GET" else client.post
    kwargs: dict[str, Any] = {"json": payload} if payload is not None else {}

    # Warm up: the first request per route pays for route indexing, the encoder's
    # first pass and any lazily built dependency.
    for _ in range(min(20, n)):
        call(path, **kwargs)

    samples: list[float] = []
    errors = 0
    for _ in range(n):
        started = time.perf_counter()
        response = call(path, **kwargs)
        samples.append((time.perf_counter() - started) * 1000.0)
        if response.status_code >= 400:
            errors += 1

    return {
        "endpoint": name,
        "requests": n,
        "errors": errors,
        "p50_ms": round(_percentile(samples, 50), 3),
        "p95_ms": round(_percentile(samples, 95), 3),
        "p99_ms": round(_percentile(samples, 99), 3),
        "mean_ms": round(statistics.fmean(samples), 3),
        "max_ms": round(max(samples), 3),
        "rps": round(1000.0 / statistics.fmean(samples), 1),
    }


SCENARIOS: list[tuple[str, str, str, dict[str, Any] | None]] = [
    ("GET /health", "GET", "/health", None),
    ("GET /v1/search", "GET", "/v1/search?q=benchmark+query&max_results=10", None),
    ("POST /v1/search", "POST", "/v1/search", {"q": "benchmark query", "max_results": 10}),
    (
        "POST /v1/crawl",
        "POST",
        "/v1/crawl",
        {"urls": [f"https://example.com/{i}" for i in range(5)]},
    ),
    ("POST /v1/answer", "POST", "/v1/answer", {"query": "what is a benchmark?"}),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=300, help="Requests per endpoint.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = parser.parse_args()

    app.dependency_overrides[get_searxng] = FakeSearxng
    app.dependency_overrides[get_crawler] = FakeCrawler
    app.dependency_overrides[get_llm] = FakeLLM
    try:
        with TestClient(app) as client:
            results = [
                _measure(client, name, method, path, payload, args.requests)
                for name, method, path, payload in SCENARIOS
            ]
    finally:
        app.dependency_overrides.clear()

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"\nIn-process benchmark -- {args.requests} requests/endpoint, dependencies faked")
    print("Measures the app's own overhead only; see loadtest/ for end-to-end numbers.\n")
    header = f"{'endpoint':<22}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'req/s':>9}{'errors':>8}"
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r['endpoint']:<22}{r['p50_ms']:>9}{r['p95_ms']:>9}"
            f"{r['p99_ms']:>9}{r['rps']:>9}{r['errors']:>8}"
        )
    print()
    return 1 if any(r["errors"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
