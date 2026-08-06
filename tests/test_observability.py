"""Phase 12 (§7.1–§7.3): structured logs, request correlation, Prometheus metrics."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx
from prometheus_client import REGISTRY
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from app.api.middleware import RequestContextMiddleware, reset_rate_limiter
from app.config import Settings, get_settings
from app.core.logging import JsonFormatter, RequestIdFilter, configure_logging, request_id_var
from app.core.search.searxng import SearxngClient
from app.main import app
from app.models.search import SearchRequest

SEARXNG_JSON = {
    "query": "python",
    "results": [{"url": "https://python.org", "title": "Python", "content": "Official."}],
    "suggestions": [],
    "answers": [],
}


class _Capture(logging.Handler):
    """Collects records, with the request-id filter the real handler carries."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.addFilter(RequestIdFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured():
    """Capture everything on the root logger for the duration of a test."""
    handler = _Capture()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


# -- §7.1 structured logs --------------------------------------------------


def test_json_formatter_emits_one_parsable_object_per_record() -> None:
    record = logging.LogRecord(
        "unified_search_api.test", logging.WARNING, __file__, 12, "hello %s", ("world",), None
    )
    record.request_id = "req-1"
    record.duration_ms = 4.5

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "unified_search_api.test"
    assert payload["message"] == "hello world"
    assert payload["request_id"] == "req-1"
    # `extra={...}` fields are merged in, which is how the access line carries
    # its method/path/status/duration.
    assert payload["duration_ms"] == 4.5
    assert payload["timestamp"].startswith("20")
    assert "\n" not in JsonFormatter().format(record)


def test_json_formatter_includes_the_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        logger = logging.getLogger("unified_search_api.test")
        record = logger.makeRecord(
            logger.name, logging.ERROR, __file__, 1, "failed", None, __import__("sys").exc_info()
        )

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exception"]


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    configure_logging("INFO", "json")
    after_first = len(root.handlers)
    configure_logging("INFO", "text")
    assert len(root.handlers) == after_first
    configure_logging("INFO", "json")  # restore the suite's default


def test_log_format_defaults_to_json() -> None:
    assert Settings().log_format == "json"


def test_request_id_reaches_a_logger_deep_in_the_handler(captured) -> None:
    """The whole point of the contextvar: no call site threads the id through."""
    deep = logging.getLogger("unified_search_api.test.deep")

    async def endpoint(scope, receive, send):
        deep.info("something happened far from the middleware")
        await PlainTextResponse("ok")(scope, receive, send)

    client = TestClient(RequestContextMiddleware(endpoint))
    resp = client.get("/", headers={"X-Request-ID": "deep-42"})

    assert resp.headers["X-Request-ID"] == "deep-42"
    deep_records = [r for r in captured.records if r.name == "unified_search_api.test.deep"]
    assert deep_records, "the handler's own log line was not captured"
    assert deep_records[0].request_id == "deep-42"
    # And the access line for the same request carries the same id.
    access = [r for r in captured.records if r.name == "unified_search_api.access"]
    assert access and access[0].request_id == "deep-42"
    assert access[0].status_code == 200
    assert access[0].path == "/"
    assert access[0].duration_ms >= 0


def test_request_id_does_not_leak_after_the_request() -> None:
    async def endpoint(scope, receive, send):
        await PlainTextResponse("ok")(scope, receive, send)

    client = TestClient(RequestContextMiddleware(endpoint))
    client.get("/", headers={"X-Request-ID": "transient"})

    assert request_id_var.get() is None


def test_access_log_line_is_emitted_per_request(captured) -> None:
    with TestClient(app) as client:
        client.get("/health")

    access = [r for r in captured.records if r.name == "unified_search_api.access"]
    assert any(r.path == "/health" and r.method == "GET" and r.status_code == 200 for r in access)


# -- §7.2 metrics ----------------------------------------------------------


def _sample(name: str, **labels: str) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value


def test_metrics_endpoint_serves_the_prometheus_exposition() -> None:
    with TestClient(app) as client:
        resp = client.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in resp.text
    assert "crawler_browser_sessions_active" in resp.text


def test_http_counter_moves_with_real_traffic() -> None:
    labels = {"method": "GET", "route": "/health", "status_code": "200"}
    before = _sample("http_requests_total", **labels)

    with TestClient(app) as client:
        client.get("/health")
        client.get("/health")

    assert _sample("http_requests_total", **labels) == before + 2
    # The histogram observed the same two requests.
    assert _sample("http_request_duration_seconds_count", **labels) >= 2


def test_route_label_uses_the_template_not_the_raw_path() -> None:
    with TestClient(app) as client:
        client.get("/v1/jobs/11111111-1111-1111-1111-111111111111")
        client.get("/v1/jobs/22222222-2222-2222-2222-222222222222")

    labels = {"method": "GET", "route": "/v1/jobs/{job_id}", "status_code": "404"}
    assert _sample("http_requests_total", **labels) >= 2
    # No per-job time series was created.
    body = TestClient(app).get("/metrics").text
    assert "11111111-1111-1111-1111-111111111111" not in body


def test_unmatched_paths_share_one_label() -> None:
    with TestClient(app) as client:
        client.get("/definitely-not-a-route")

    assert _sample("http_requests_total", method="GET", route="unmatched", status_code="404") >= 1


@respx.mock
@pytest.mark.asyncio
async def test_search_cache_counter_records_hit_and_miss() -> None:
    respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    hits = _sample("cache_requests_total", cache="search", outcome="hit")
    misses = _sample("cache_requests_total", cache="search", outcome="miss")

    client = SearxngClient("http://searxng:8080", cache_ttl=60.0)
    try:
        await client.search(SearchRequest(q="python", max_results=5))
        await client.search(SearchRequest(q="python", max_results=5))
    finally:
        await client.aclose()

    assert _sample("cache_requests_total", cache="search", outcome="miss") == misses + 1
    assert _sample("cache_requests_total", cache="search", outcome="hit") == hits + 1


@pytest.mark.asyncio
async def test_crawl_counter_and_session_gauge_move_with_a_crawl() -> None:
    """The gauge has to be read *during* the crawl: it is back to 0 afterwards."""
    from types import SimpleNamespace

    from app.core.crawl.crawler import CrawlerService
    from app.core.net.guard import UrlGuard
    from app.models.crawl import CrawlRequest

    successes = _sample("crawl_pages_total", outcome="success")
    failures = _sample("crawl_pages_total", outcome="failure")
    seen_active: list[float] = []

    class _StubBrowser:
        async def start(self) -> None: ...

        async def close(self) -> None: ...

        async def arun(self, url: str, config=None):
            seen_active.append(_sample("crawler_browser_sessions_active"))
            if "bad" in url:
                return SimpleNamespace(success=False, url=url, error_message="nope")
            return SimpleNamespace(
                success=True,
                url=url,
                status_code=200,
                markdown=SimpleNamespace(raw_markdown="body", fit_markdown="body"),
                metadata={"title": "T"},
                links={"internal": [], "external": []},
            )

    service = CrawlerService(guard=UrlGuard(allow_private=True), per_host_delay=0.0)
    service._crawler = _StubBrowser()
    await service.crawl(CrawlRequest(urls=["https://ok.example/a", "https://bad.example/b"]))

    assert max(seen_active) >= 1  # a slot was held while the page was fetched
    assert _sample("crawler_browser_sessions_active") == 0  # and released after
    assert _sample("crawl_pages_total", outcome="success") == successes + 1
    assert _sample("crawl_pages_total", outcome="failure") == failures + 1


def test_llm_usage_counters_take_the_delta_not_the_total() -> None:
    from app.core.llm.usage import UsageTracker
    from app.core.metrics import record_llm_usage

    usage = UsageTracker(prompt_tokens=10, completion_tokens=4, total_cost_usd=0.5)
    before = _sample("llm_tokens_total", label="metrics-test", kind="prompt")

    record_llm_usage("metrics-test", usage)
    record_llm_usage("metrics-test", usage)  # same totals: must add nothing
    usage.prompt_tokens += 3
    record_llm_usage("metrics-test", usage)

    assert _sample("llm_tokens_total", label="metrics-test", kind="prompt") == before + 13
    assert _sample("llm_cost_usd_total", label="metrics-test") == pytest.approx(0.5)


def test_jobs_gauge_is_sampled_from_the_live_job_table() -> None:
    from app.jobs.manager import Job, JobManager, JobStatus

    manager = JobManager()
    manager._jobs["a"] = Job(id="a", type="research", status=JobStatus.running)
    manager._jobs["b"] = Job(id="b", type="research", status=JobStatus.completed)
    manager._jobs["c"] = Job(id="c", type="site_crawl", status=JobStatus.pending)

    assert manager.active_counts() == {"research": 1, "site_crawl": 1}


def test_metrics_requires_an_api_key_like_every_other_endpoint(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", "scraper:s3cret-scrape-key")
    get_settings.cache_clear()

    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        ok = client.get("/metrics", headers={"X-API-Key": "s3cret-scrape-key"})

    assert ok.status_code == 200
    assert "http_requests_total" in ok.text


def test_metrics_is_exempt_from_rate_limiting(monkeypatch) -> None:
    get_settings.cache_clear()
    reset_rate_limiter()
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_RPM", "1")
    get_settings.cache_clear()

    with TestClient(app) as client:
        # A scrape must never spend a caller's budget, nor be throttled into
        # holes in the time series.
        for _ in range(5):
            assert client.get("/metrics").status_code == 200
        # Sanity check that the limiter really is on: `/` is not exempt.
        assert client.get("/").status_code == 200
        assert client.get("/").status_code == 429


# -- §7.3 tracing (optional, best-effort) ----------------------------------


def test_tracing_is_off_by_default() -> None:
    assert Settings().otel_enabled is False


def test_setup_tracing_never_raises_when_the_sdk_is_unavailable(monkeypatch) -> None:
    """The startup contract: a broken tracing setup degrades, it does not abort."""
    import app.core.tracing as tracing

    monkeypatch.setattr(tracing, "_started", False)
    monkeypatch.setitem(__import__("sys").modules, "opentelemetry.sdk.trace", None)

    assert tracing.setup_tracing(object()) is False


def test_setup_tracing_instruments_a_fastapi_app() -> None:
    from fastapi import FastAPI
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    import app.core.tracing as tracing

    tracing._started = False
    target = FastAPI()
    try:
        assert tracing.setup_tracing(target, service_name="test") is True
    finally:
        # Both are process-wide patches; leaving them on would follow every other
        # test in the session around.
        FastAPIInstrumentor.uninstrument_app(target)
        HTTPXClientInstrumentor().uninstrument()
        tracing._started = False
