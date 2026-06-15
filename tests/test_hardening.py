from starlette.testclient import TestClient

from app.api.middleware import reset_rate_limiter
from app.config import get_settings
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
            assert client.get("/").status_code == 200
            assert client.get("/").status_code == 200
            limited = client.get("/")
            assert limited.status_code == 429
            assert "Retry-After" in limited.headers
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
