"""GET /health/ready (§5.9): healthy and each degraded case."""

from __future__ import annotations

import httpx
import respx
from starlette.testclient import TestClient

from app.api.routes.health import _check_llm
from app.config import Settings, get_settings
from app.main import app


def _healthy_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()


def test_liveness_is_unchanged_and_depends_on_nothing() -> None:
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "checks" not in resp.json()


@respx.mock
def test_ready_when_every_dependency_is_healthy(monkeypatch) -> None:
    _healthy_env(monkeypatch)
    respx.get("http://searxng:8080/healthz").mock(return_value=httpx.Response(200, text="OK"))
    with TestClient(app) as client:
        resp = client.get("/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert {c["status"] for c in body["checks"].values()} == {"ok"}


@respx.mock
def test_503_when_searxng_is_unreachable(monkeypatch) -> None:
    _healthy_env(monkeypatch)
    respx.get("http://searxng:8080/healthz").mock(side_effect=httpx.ConnectError("refused"))
    with TestClient(app) as client:
        resp = client.get("/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["failed"] == ["searxng"]
    assert body["checks"]["searxng"]["status"] == "error"
    # The other checks still report, so an operator sees the whole picture.
    assert body["checks"]["crawler"]["status"] == "ok"


@respx.mock
def test_503_when_searxng_returns_5xx(monkeypatch) -> None:
    _healthy_env(monkeypatch)
    respx.get("http://searxng:8080/healthz").mock(return_value=httpx.Response(502))
    with TestClient(app) as client:
        resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["failed"] == ["searxng"]


@respx.mock
def test_503_when_no_provider_key_is_configured(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-4")
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    respx.get("http://searxng:8080/healthz").mock(return_value=httpx.Response(200))

    with TestClient(app) as client:
        resp = client.get("/health/ready")

    assert resp.status_code == 503
    assert resp.json()["failed"] == ["llm"]
    assert "ANTHROPIC_API_KEY" in resp.json()["checks"]["llm"]["detail"]


def test_crawler_check_reports_the_lazy_pool_as_ready() -> None:
    """A pool that has not started yet would start fine — probing must not launch one."""
    from app.api.routes.health import _check_crawler

    class FakeRequest:
        class app:
            class state:
                crawler = type("C", (), {"started": False})()

    check = _check_crawler(FakeRequest())  # type: ignore[arg-type]
    assert check["status"] == "ok"
    assert "not started yet" in check["detail"]


def test_llm_check_accepts_a_keyless_provider(monkeypatch) -> None:
    settings = Settings(llm_model="ollama/llama3")
    assert _check_llm(settings)["status"] == "ok"


def test_llm_check_falls_back_to_any_configured_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings = Settings(llm_model="some-unknown-provider/model")
    check = _check_llm(settings)
    assert check["status"] == "ok"
    assert "OPENAI_API_KEY" in check["detail"]
