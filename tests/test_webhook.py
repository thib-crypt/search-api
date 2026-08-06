"""Job-completion webhooks (§5.8).

Three properties: the receiver can verify the payload, the callback target goes
through the SSRF guard like any other server-fetched URL, and a webhook that
fails cannot take the job down with it.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from starlette.testclient import TestClient

from app.api.deps import get_crawler, get_jobs, get_llm, get_reranker, get_searxng
from app.config import get_settings
from app.core.net.webhook import SIGNATURE_HEADER, deliver, sign
from app.core.research.rerank import NoopReranker
from app.jobs.manager import JobManager, JobStatus
from app.main import app
from tests.test_research import FakeCrawler, FakeLLM, FakeSearxng

SECRET = "whsec_" + "0" * 32


class _FakeHttpx:
    """Stands in for the `httpx` module *inside the webhook module only*.

    Patching `httpx.AsyncClient` globally would also replace the client the app's
    own SearXNG connection is built from, which then fails on shutdown.
    """

    def __init__(self, client_cls) -> None:
        self.AsyncClient = client_cls


def test_signature_verifies_against_the_raw_body() -> None:
    body = b'{"job_id":"abc","status":"completed"}'
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert sign(body, SECRET) == f"sha256={expected}"
    # A different secret (or a tampered body) must not verify.
    assert sign(body, "other-secret") != sign(body, SECRET)
    assert sign(body + b" ", SECRET) != sign(body, SECRET)


async def test_delivery_signs_the_exact_bytes_it_sends(monkeypatch) -> None:
    seen: dict = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            seen["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def post(self, url, *, content, headers):
            seen["url"] = url
            seen["body"] = content
            seen["headers"] = headers
            return httpx.Response(200)

    monkeypatch.setattr("app.core.net.webhook.httpx", _FakeHttpx(FakeClient))

    ok = await deliver(
        "https://hooks.example.com/x",
        {"job_id": "abc", "status": "completed"},
        secret=SECRET,
        timeout_seconds=3.0,
    )

    assert ok is True
    assert seen["url"] == "https://hooks.example.com/x"
    assert seen["timeout"] == 3.0
    # The receiver recomputes the HMAC over the body it received, so the
    # signature must cover exactly those bytes — not a re-serialization of them.
    assert seen["headers"][SIGNATURE_HEADER] == sign(seen["body"], SECRET)
    assert json.loads(seen["body"])["job_id"] == "abc"


async def test_notify_rechecks_the_guard_at_delivery_time(monkeypatch) -> None:
    """A URL can start clean and go bad by the time a long-running job finishes.

    `_check_webhook` (the route's submission-time check) only runs once. A
    background run can take minutes, long enough for the target host's DNS to
    be repointed at a private address in between — the same rebinding window
    `CrawlerService._guard_landing` closes for crawled redirects. `_notify`
    must not trust the submission-time result; it re-runs the guard right
    before the actual fetch.
    """
    from app.api.routes.research import _notify
    from app.core.net.guard import BlockedUrlError

    class GuardThatChangedItsMind:
        async def check(self, url: str) -> None:
            raise BlockedUrlError("host now resolves to a private address")

    sent: list[dict] = []

    async def fake_deliver(url, payload, *, secret, timeout_seconds=10.0):
        sent.append(payload)
        return True

    monkeypatch.setattr("app.api.routes.research.deliver", fake_deliver)

    await _notify(
        "https://hooks.example.com/x",
        get_settings(),
        {"job_id": "abc", "status": "completed"},
        GuardThatChangedItsMind(),
    )

    assert sent == []  # deliver() must never be reached once the recheck fails


async def test_an_unreachable_receiver_is_reported_not_raised(monkeypatch) -> None:
    class DeadClient:
        def __init__(self, **kwargs) -> None: ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def post(self, *a, **kw):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("app.core.net.webhook.httpx", _FakeHttpx(DeadClient))
    assert await deliver("https://nope.example.com", {}, secret=SECRET) is False


async def test_a_rejecting_receiver_is_reported_not_raised(monkeypatch) -> None:
    class RejectingClient:
        def __init__(self, **kwargs) -> None: ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def post(self, *a, **kw):
            return httpx.Response(500)

    monkeypatch.setattr("app.core.net.webhook.httpx", _FakeHttpx(RejectingClient))
    assert await deliver("https://nope.example.com", {}, secret=SECRET) is False


# -- Through the endpoint ---------------------------------------------------


@pytest.fixture
def research_app(monkeypatch):
    jobs = JobManager()
    app.dependency_overrides[get_searxng] = lambda: FakeSearxng()
    app.dependency_overrides[get_crawler] = lambda: FakeCrawler()
    app.dependency_overrides[get_llm] = lambda: FakeLLM(feedback_questions=[])
    app.dependency_overrides[get_reranker] = lambda: NoopReranker()
    app.dependency_overrides[get_jobs] = lambda: jobs
    get_settings.cache_clear()
    yield jobs
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _body(**extra) -> dict:
    return {
        "query": "x",
        "depth": 1,
        "breadth": 1,
        "interactive": False,
        "background": True,
        **extra,
    }


def test_an_ssrf_blocked_webhook_url_is_refused(research_app, monkeypatch) -> None:
    """The callback target is a client-supplied URL the *server* fetches (§2.1)."""
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE_NETWORKS", "false")
    get_settings.cache_clear()
    with TestClient(app) as client:
        resp = client.post(
            "/v1/research", json=_body(webhook_url="http://169.254.169.254/latest/meta-data")
        )
    assert resp.status_code == 400
    assert "webhook_url not allowed" in resp.json()["detail"]
    # Refused before a job exists, so nothing is left half-created.
    assert not research_app._jobs


def test_a_non_http_webhook_url_is_refused(research_app, monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    with TestClient(app) as client:
        resp = client.post("/v1/research", json=_body(webhook_url="file:///etc/passwd"))
    assert resp.status_code == 400


def test_a_webhook_without_a_configured_secret_is_refused(research_app, monkeypatch) -> None:
    """An unsigned callback cannot be authenticated, so it is not offered at all."""
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    with TestClient(app) as client:
        resp = client.post("/v1/research", json=_body(webhook_url="https://hooks.example.com/x"))
    assert resp.status_code == 400
    assert "WEBHOOK_SECRET" in resp.json()["detail"]


def test_a_completed_job_fires_a_signed_callback(research_app, monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE_NETWORKS", "true")  # so example.com need not resolve
    get_settings.cache_clear()

    sent: list[dict] = []

    async def fake_deliver(url, payload, *, secret, timeout_seconds=10.0):
        sent.append({"url": url, "payload": payload, "secret": secret})
        return True

    monkeypatch.setattr("app.api.routes.research.deliver", fake_deliver)

    with TestClient(app) as client:
        resp = client.post("/v1/research", json=_body(webhook_url="https://hooks.example.com/x"))
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        _drain(client, job_id)

    assert len(sent) == 1
    assert sent[0]["url"] == "https://hooks.example.com/x"
    assert sent[0]["secret"] == SECRET
    payload = sent[0]["payload"]
    assert payload["job_id"] == job_id
    assert payload["status"] == "completed"
    assert payload["result"]["report"]
    assert "usage" in payload["result"]


def test_a_failing_webhook_does_not_fail_the_job(research_app, monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE_NETWORKS", "true")
    get_settings.cache_clear()

    async def dead_client(*a, **kw):
        raise httpx.ConnectError("no route to host")

    class DeadClient:
        def __init__(self, **kwargs) -> None: ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        post = dead_client

    monkeypatch.setattr("app.core.net.webhook.httpx", _FakeHttpx(DeadClient))

    with TestClient(app) as client:
        resp = client.post("/v1/research", json=_body(webhook_url="https://hooks.example.com/x"))
        job_id = resp.json()["job_id"]
        _drain(client, job_id)

    job = research_app.get(job_id)
    assert job is not None
    assert job.status is JobStatus.completed  # the run stands on its own
    assert job.result["report"]


def test_a_failed_job_still_notifies(research_app, monkeypatch) -> None:
    """A client waiting on a callback needs to hear about failures too."""
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE_NETWORKS", "true")
    get_settings.cache_clear()

    class BrokenLLM(FakeLLM):
        async def structured(self, messages, response_model, *, model=None, **kwargs):
            if response_model.__name__ == "SerpQueryList":
                raise RuntimeError("provider is down")
            return await super().structured(messages, response_model, model=model, **kwargs)

    app.dependency_overrides[get_llm] = lambda: BrokenLLM(feedback_questions=[])

    sent: list[dict] = []

    async def fake_deliver(url, payload, *, secret, timeout_seconds=10.0):
        sent.append(payload)
        return True

    monkeypatch.setattr("app.api.routes.research.deliver", fake_deliver)

    with TestClient(app) as client:
        resp = client.post("/v1/research", json=_body(webhook_url="https://hooks.example.com/x"))
        job_id = resp.json()["job_id"]
        _drain(client, job_id)

    assert len(sent) == 1
    assert sent[0]["status"] == "failed"
    assert "provider is down" in sent[0]["error"]
    assert research_app.get(job_id).status is JobStatus.failed


def test_no_webhook_url_means_no_callback(research_app, monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    sent: list = []
    monkeypatch.setattr(
        "app.api.routes.research.deliver",
        lambda *a, **kw: sent.append(a) or _true(),
    )
    with TestClient(app) as client:
        resp = client.post("/v1/research", json=_body())
        _drain(client, resp.json()["job_id"])
    assert sent == []


async def _true() -> bool:  # pragma: no cover - only reached if the guard regresses
    return True


def _drain(client, job_id: str) -> None:
    """Block until the background job is done, via its own SSE channel."""
    with client.stream("GET", f"/v1/jobs/{job_id}/stream") as stream:
        for _ in stream.iter_lines():
            pass
