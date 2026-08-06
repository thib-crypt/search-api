"""API-key auth (§2.8, §2.13).

The point of these is that an unauthenticated instance cannot happen by accident:
`/v1/research` spends real provider credits per call.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.api.deps import _key_accepted
from app.config import Settings, get_settings
from app.main import app

KEY = "sk_test_" + "a" * 32
OTHER = "sk_test_" + "b" * 32


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", f"{KEY},{OTHER}")
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def test_request_without_a_key_is_rejected(authed) -> None:
    resp = authed.get("/v1/jobs/whatever")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_request_with_a_wrong_key_is_rejected(authed) -> None:
    assert authed.get("/v1/jobs/whatever", headers={"X-API-Key": "nope"}).status_code == 401


def test_x_api_key_header_is_accepted(authed) -> None:
    # 404, not 401: the request got past auth and simply names no known job.
    assert authed.get("/v1/jobs/whatever", headers={"X-API-Key": KEY}).status_code == 404


def test_bearer_token_is_accepted(authed) -> None:
    resp = authed.get("/v1/jobs/whatever", headers={"Authorization": f"Bearer {OTHER}"})
    assert resp.status_code == 404


def test_health_stays_open(authed) -> None:
    assert authed.get("/health").status_code == 200


def test_research_requires_a_key(authed) -> None:
    resp = authed.post("/v1/research", json={"query": "x"})
    assert resp.status_code == 401


def test_startup_is_refused_when_auth_is_on_with_no_keys(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="API_KEYS"), TestClient(app):
            pass  # pragma: no cover - startup raises before the body runs
    finally:
        get_settings.cache_clear()


def test_validate_startup_accepts_auth_off() -> None:
    Settings(auth_enabled=False, api_keys="").validate_startup()


def test_key_comparison_checks_every_configured_key() -> None:
    keys = {KEY, OTHER}
    assert _key_accepted(KEY, keys)
    assert _key_accepted(OTHER, keys)
    assert not _key_accepted("sk_test_" + "a" * 31, keys)  # prefix of a valid key
    assert not _key_accepted("", keys)
    assert not _key_accepted(KEY, set())


def test_auth_disabled_outside_development_warns(monkeypatch, caplog) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    try:
        with caplog.at_level("WARNING"), TestClient(app):
            pass
        assert any("AUTH_ENABLED is false" in r.message for r in caplog.records)
    finally:
        get_settings.cache_clear()
