import anyio
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from app.api.middleware import RequestContextMiddleware, path_weight, reset_rate_limiter
from app.config import Settings, get_settings
from app.main import app


def test_security_headers_and_request_id() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "X-Request-ID" in resp.headers


def test_request_id_is_echoed() -> None:
    with TestClient(app) as client:
        resp = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["X-Request-ID"] == "abc-123"


def test_request_id_is_readable_downstream_as_request_state() -> None:
    """The pure-ASGI middleware writes through scope["state"].

    app/main.py's unhandled-exception handler logs getattr(request.state,
    "request_id", None); if this regresses it fails silently (None in the logs).
    """
    seen: dict[str, object] = {}

    async def endpoint(scope, receive, send):
        seen["request_id"] = getattr(Request(scope).state, "request_id", None)
        await PlainTextResponse("ok")(scope, receive, send)

    # No `with`: the bare endpoint below speaks http only, not lifespan.
    client = TestClient(RequestContextMiddleware(endpoint))
    resp = client.get("/", headers={"X-Request-ID": "downstream-1"})

    assert seen["request_id"] == "downstream-1"
    assert resp.headers["X-Request-ID"] == "downstream-1"


def test_non_http_scopes_pass_through_untouched() -> None:
    calls: list[str] = []

    async def endpoint(scope, receive, send):
        calls.append(scope["type"])

    anyio.run(RequestContextMiddleware(endpoint), {"type": "lifespan"}, None, None)
    assert calls == ["lifespan"]


def test_root_metadata() -> None:
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"]
    assert "/v1/research" in body["endpoints"]


def test_rate_limit_returns_429(monkeypatch) -> None:
    get_settings.cache_clear()
    reset_rate_limiter()
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_RPM", "2")
    try:
        with TestClient(app) as client:
            # `/` is unweighted (1 point), so a budget of 2 buys exactly two calls.
            assert client.get("/").status_code == 200
            assert client.get("/").status_code == 200
            limited = client.get("/")
            assert limited.status_code == 429
            assert "Retry-After" in limited.headers
            # The limiter emits raw ASGI messages rather than a JSONResponse;
            # the body and content-type must stay identical to what clients saw.
            assert limited.headers["content-type"] == "application/json"
            assert limited.content == b'{"detail":"Rate limit exceeded."}'
            assert limited.json() == {"detail": "Rate limit exceeded."}
    finally:
        reset_rate_limiter()
        get_settings.cache_clear()


def test_rate_limit_uses_forwarded_for_when_trusted_proxy(monkeypatch) -> None:
    get_settings.cache_clear()
    reset_rate_limiter()
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_RPM", "1")
    monkeypatch.setenv("TRUSTED_PROXY", "true")
    try:
        with TestClient(app) as client:
            # Two different X-Forwarded-For IPs should each get their own quota.
            r1 = client.get("/", headers={"X-Forwarded-For": "1.2.3.4"})
            r2 = client.get("/", headers={"X-Forwarded-For": "5.6.7.8"})
            assert r1.status_code == 200
            assert r2.status_code == 200
            # Third request from same IP as r1 → limited.
            r3 = client.get("/", headers={"X-Forwarded-For": "1.2.3.4"})
            assert r3.status_code == 429
    finally:
        reset_rate_limiter()
        get_settings.cache_clear()


# -- Weighted rate limiting (§6.3) ------------------------------------------


@pytest.mark.parametrize(
    ("path", "weight"),
    [
        ("/v1/search", 1),
        ("/v1/map", 2),
        ("/v1/crawl", 5),
        ("/v1/answer", 10),
        ("/v1/extract", 10),
        ("/v1/crawl/site", 25),  # must not be priced as /v1/crawl
        ("/v1/research", 100),
        ("/v1/jobs/abc/stream", 1),  # anything unlisted
        ("/", 1),
    ],
)
def test_endpoint_weights(path: str, weight: int) -> None:
    assert path_weight(path) == weight


def test_an_expensive_endpoint_burns_the_budget_faster(monkeypatch) -> None:
    """The whole point of §6.3: one /v1/research must not cost the same as one /v1/search."""
    get_settings.cache_clear()
    reset_rate_limiter()
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_RPM", "100")
    try:
        with TestClient(app) as client:
            # A single research call (weight 100) fills a budget of 100 exactly.
            # `estimate_only` keeps the test off the network; the limiter runs
            # before routing and cannot tell the difference.
            first = client.post("/v1/research", json={"query": "x", "estimate_only": True})
            assert first.status_code == 200
            assert first.headers["RateLimit-Remaining"] == "0"
            # Even the cheapest endpoint is now refused.
            assert client.get("/v1/search?q=x").status_code == 429
    finally:
        reset_rate_limiter()
        get_settings.cache_clear()


def test_rate_limit_headers_are_on_successful_responses(monkeypatch) -> None:
    """A client can only pace itself if it sees the budget while it still has one."""
    get_settings.cache_clear()
    reset_rate_limiter()
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_RPM", "50")
    try:
        with TestClient(app) as client:
            resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["RateLimit-Limit"] == "50"
        assert resp.headers["RateLimit-Remaining"] == "49"
        assert 0 < int(resp.headers["RateLimit-Reset"]) <= 60
    finally:
        reset_rate_limiter()
        get_settings.cache_clear()


def test_authenticated_callers_are_bucketed_by_label_not_by_ip(monkeypatch) -> None:
    """Two keys share one IP in tests; one exhausting its budget must not limit the other."""
    get_settings.cache_clear()
    reset_rate_limiter()
    key_a = "sk_" + "a" * 32
    key_b = "sk_" + "b" * 32
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_RPM", "2")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", f"app-a:{key_a},app-b:{key_b}")
    try:
        with TestClient(app) as client:
            a = {"X-API-Key": key_a}
            b = {"X-API-Key": key_b}
            assert client.get("/v1/jobs/nope", headers=a).status_code == 404
            assert client.get("/v1/jobs/nope", headers=a).status_code == 404
            assert client.get("/v1/jobs/nope", headers=a).status_code == 429
            # app-b's budget is untouched.
            assert client.get("/v1/jobs/nope", headers=b).status_code == 404
    finally:
        reset_rate_limiter()
        get_settings.cache_clear()


def test_a_per_label_override_replaces_the_global_budget(monkeypatch) -> None:
    get_settings.cache_clear()
    reset_rate_limiter()
    key = "sk_" + "c" * 32
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_RPM", "1")
    monkeypatch.setenv("RATE_LIMIT_RPM_OVERRIDES", "generous:5")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", f"generous:{key}")
    try:
        with TestClient(app) as client:
            headers = {"X-API-Key": key}
            for _ in range(5):
                assert client.get("/v1/jobs/nope", headers=headers).status_code == 404
            assert client.get("/v1/jobs/nope", headers=headers).status_code == 429
    finally:
        reset_rate_limiter()
        get_settings.cache_clear()


def test_override_parsing_ignores_malformed_entries() -> None:
    parsed = Settings(
        rate_limit_rpm_overrides="app:500, test:20 ,broken,x:nope,:9,"
    ).rate_limit_overrides
    assert parsed == {"app": 500, "test": 20}


def test_health_probes_are_never_rate_limited(monkeypatch) -> None:
    """An orchestrator polling readiness must not eat a caller's budget."""
    get_settings.cache_clear()
    reset_rate_limiter()
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_RPM", "1")
    try:
        with TestClient(app) as client:
            for _ in range(5):
                assert client.get("/health").status_code == 200
                assert client.get("/health/ready").status_code in (200, 503)
    finally:
        reset_rate_limiter()
        get_settings.cache_clear()
