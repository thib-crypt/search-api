"""Load test for /v1/search and /v1/answer against a real deployment (§8).

This is a tool for a human to point at a running instance -- it is deliberately
not wired into CI, because a meaningful run needs a real SearXNG, a real browser
pool and a real LLM provider, none of which a CI runner has. For the app's own
overhead with those dependencies faked out, use `scripts/bench.py` instead; the
two answer different questions.

    uv sync --extra loadtest
    LOCUST_API_KEY=sk-... uv run locust -f loadtest/locustfile.py --host http://localhost:8000

See loadtest/README.md for load shapes and what to watch.
"""

from __future__ import annotations

import os
import random

from locust import HttpUser, between, events, tag, task

API_KEY = os.environ.get("LOCUST_API_KEY", "")

# Distinct enough to exercise cache misses; the search cache is keyed on the
# whole query, so hammering one term would measure the cache and nothing else.
SEARCH_QUERIES = [
    "postgres connection pooling",
    "rust async runtime comparison",
    "kubernetes ingress vs gateway api",
    "sqlite wal mode concurrency",
    "openapi code generation typescript",
    "vector database hnsw index",
    "python asyncio event loop blocking",
    "prometheus histogram buckets",
]

ANSWER_QUERIES = [
    "What is the difference between HNSW and IVFFlat?",
    "How does SQLite WAL mode handle concurrent readers?",
    "When should I use a gateway API over an ingress?",
    "Why does asyncio block on CPU-bound work?",
]


@events.test_start.add_listener
def _warn_without_key(environment, **_kwargs) -> None:
    if not API_KEY:
        print(
            "LOCUST_API_KEY is unset. Against an instance with AUTH_ENABLED=true "
            "every request will 401 and the numbers will be meaningless."
        )


class SearchUser(HttpUser):
    """The cheap, high-volume endpoint. Most traffic looks like this."""

    weight = 9
    # A real client thinks between calls; zero wait would measure how fast locust
    # can spin rather than how the service behaves under a given concurrency.
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        if API_KEY:
            self.client.headers.update({"X-API-Key": API_KEY})

    @tag("search")
    @task(4)
    def search_post(self) -> None:
        self.client.post(
            "/v1/search",
            json={"q": random.choice(SEARCH_QUERIES), "max_results": 10},
            name="POST /v1/search",
        )

    @tag("search")
    @task(1)
    def search_get(self) -> None:
        self.client.get(
            "/v1/search",
            params={"q": random.choice(SEARCH_QUERIES), "max_results": 10},
            name="GET /v1/search",
        )

    @tag("health")
    @task(1)
    def health(self) -> None:
        # Exempt from rate limiting and auth; here to show whether the event loop
        # still answers promptly while the expensive endpoints are saturated.
        # A rising p95 on *this* is the single clearest sign of loop blocking.
        self.client.get("/health", name="GET /health")


class AnswerUser(HttpUser):
    """The expensive endpoint: search + crawl + LLM, and real provider spend.

    Weighted far below search on purpose. Note that a serious run against a real
    provider costs real money, and that `/v1/answer` is priced at 10 rate-limit
    points against /v1/search's 1 -- so a flat mix would hit the limiter long
    before it hit the service.
    """

    weight = 1
    wait_time = between(5.0, 15.0)

    def on_start(self) -> None:
        if API_KEY:
            self.client.headers.update({"X-API-Key": API_KEY})

    @tag("answer")
    @task
    def answer(self) -> None:
        with self.client.post(
            "/v1/answer",
            json={"query": random.choice(ANSWER_QUERIES), "max_sources": 5},
            name="POST /v1/answer",
            timeout=120,
            catch_response=True,
        ) as response:
            # A 429 is the limiter doing its job, not a service failure; counting
            # it as an error would hide the real ones.
            if response.status_code == 429:
                response.success()
