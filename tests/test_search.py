import httpx
import respx
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import app

SEARXNG_JSON = {
    "query": "python",
    "results": [
        {
            "url": "https://python.org",
            "title": "Python",
            "content": "The official site.",
            "engine": "google",
            "score": 1.0,
            "category": "general",
        },
        {
            "url": "https://docs.python.org",
            "title": "Docs",
            "content": "Documentation.",
            "engine": "bing",
        },
        {"title": "no url, dropped"},
    ],
    "suggestions": ["python tutorial"],
    "answers": [],
}


@respx.mock
def test_search_normalizes_results() -> None:
    route = respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    with TestClient(app) as client:
        resp = client.post("/v1/search", json={"q": "python", "max_results": 5})

    assert route.called
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "python"
    # The result without a URL is dropped.
    assert body["number_of_results"] == 2
    assert body["results"][0]["url"] == "https://python.org"
    assert body["results"][0]["snippet"] == "The official site."
    assert body["suggestions"] == ["python tutorial"]


@respx.mock
def test_search_502_on_backend_error() -> None:
    respx.get("http://searxng:8080/search").mock(return_value=httpx.Response(403))
    with TestClient(app) as client:
        resp = client.post("/v1/search", json={"q": "python"})
    assert resp.status_code == 502


def test_auth_blocks_when_enabled(monkeypatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", "secret123")
    try:
        with TestClient(app) as client:
            unauth = client.post("/v1/search", json={"q": "python"})
            assert unauth.status_code == 401

            with respx.mock:
                respx.get("http://searxng:8080/search").mock(
                    return_value=httpx.Response(200, json=SEARXNG_JSON)
                )
                ok = client.post(
                    "/v1/search",
                    json={"q": "python"},
                    headers={"X-API-Key": "secret123"},
                )
                assert ok.status_code == 200
    finally:
        get_settings.cache_clear()
