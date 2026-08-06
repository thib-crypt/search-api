"""GET /v1/search (§5.10): parity with POST, plus HTTP cache headers."""

from __future__ import annotations

import httpx
import respx
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import app

SEARXNG_JSON = {
    "query": "python",
    "results": [
        {"url": "https://python.org", "title": "Python", "content": "Official.", "engine": "g"},
        {"url": "https://docs.python.org", "title": "Docs", "content": "Docs.", "engine": "b"},
        {"url": "https://spam.example", "title": "Spam", "content": "", "engine": "b"},
    ],
    "suggestions": ["python tutorial"],
}


@respx.mock
def test_get_returns_the_same_body_as_post() -> None:
    respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    with TestClient(app) as client:
        posted = client.post("/v1/search", json={"q": "python", "max_results": 5})
        got = client.get("/v1/search", params={"q": "python", "max_results": 5})

    assert got.status_code == 200
    assert got.json() == posted.json()


@respx.mock
def test_cache_headers_are_present() -> None:
    respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    with TestClient(app) as client:
        resp = client.get("/v1/search", params={"q": "python"})

    assert "max-age=" in resp.headers["cache-control"]
    # Auth is off in the test environment, so a shared proxy cache is safe.
    assert resp.headers["cache-control"].startswith("public")
    assert resp.headers["etag"].startswith('"')


@respx.mock
def test_matching_etag_returns_304_without_a_body() -> None:
    respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    with TestClient(app) as client:
        first = client.get("/v1/search", params={"q": "python"})
        second = client.get(
            "/v1/search", params={"q": "python"}, headers={"If-None-Match": first.headers["etag"]}
        )

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == first.headers["etag"]


@respx.mock
def test_different_queries_get_different_etags() -> None:
    respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    with TestClient(app) as client:
        a = client.get("/v1/search", params={"q": "python"})
        b = client.get("/v1/search", params={"q": "rust", "max_results": 1})
    assert a.headers["etag"] != b.headers["etag"]


@respx.mock
def test_list_params_accept_csv_and_repetition() -> None:
    route = respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    with TestClient(app) as client:
        csv = client.get("/v1/search", params={"q": "python", "categories": "general,news"})
        repeated = client.get(
            "/v1/search?q=python&categories=general&categories=news",
        )

    assert csv.status_code == repeated.status_code == 200
    sent = [str(call.request.url) for call in route.calls]
    assert all("categories=general%2Cnews" in url for url in sent)


@respx.mock
def test_domain_filters_work_on_the_get_variant() -> None:
    respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    with TestClient(app) as client:
        resp = client.get("/v1/search", params={"q": "python", "exclude_domains": "spam.example"})
    urls = [r["url"] for r in resp.json()["results"]]
    assert "https://spam.example" not in urls


@respx.mock
def test_cache_control_is_private_when_auth_is_enabled(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", "secret123")
    respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    try:
        with TestClient(app) as client:
            resp = client.get(
                "/v1/search", params={"q": "python"}, headers={"X-API-Key": "secret123"}
            )
        # An authenticated response must never be handed to another key by a
        # shared cache.
        assert resp.headers["cache-control"].startswith("private")
    finally:
        get_settings.cache_clear()


def test_get_requires_a_query() -> None:
    with TestClient(app) as client:
        resp = client.get("/v1/search")
    assert resp.status_code == 422
