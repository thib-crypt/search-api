"""Compression must shrink large JSON bodies and never touch SSE (§3.2)."""

from __future__ import annotations

import gzip

import pytest
from fastapi import FastAPI
from sse_starlette.sse import EventSourceResponse
from starlette.testclient import TestClient

from app.api.middleware import CompressionMiddleware

BIG = "lorem ipsum dolor sit amet " * 500


@pytest.fixture
def client() -> TestClient:
    """Minimal app carrying only the compression layer."""
    app = FastAPI()
    app.add_middleware(CompressionMiddleware, minimum_size=1024)

    @app.get("/big")
    async def big() -> dict[str, str]:
        return {"markdown": BIG}

    @app.get("/small")
    async def small() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/stream")
    async def stream() -> EventSourceResponse:
        async def events():
            for i in range(5):
                yield {"event": "progress", "data": f'{{"step": {i}}}'}

        return EventSourceResponse(events())

    with TestClient(app) as c:
        yield c


def test_large_json_is_gzipped(client: TestClient) -> None:
    r = client.get("/big", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in r.headers["vary"].lower()
    # httpx transparently decodes, so the body is still the original payload.
    assert r.json()["markdown"] == BIG
    # And the bytes on the wire really were compressed.
    assert int(r.headers["content-length"]) < len(BIG)


def test_small_json_is_not_gzipped(client: TestClient) -> None:
    r = client.get("/small", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers


def test_client_without_gzip_support_gets_plain_json(client: TestClient) -> None:
    r = client.get("/big", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.json()["markdown"] == BIG


def test_sse_is_never_compressed(client: TestClient) -> None:
    """Gzipping SSE would buffer the stream and break incremental delivery."""
    with client.stream("GET", "/stream", headers={"Accept-Encoding": "gzip"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in r.headers
        body = b"".join(r.iter_raw())

    # Raw bytes: plain SSE frames, not a gzip member (magic 0x1f 0x8b).
    assert not body.startswith(b"\x1f\x8b")
    assert b'data: {"step": 0}' in body
    assert b'data: {"step": 4}' in body


def test_real_app_sse_endpoint_is_not_compressed() -> None:
    """Same guarantee through the app's full middleware stack."""
    from app.api.deps import get_jobs
    from app.jobs.manager import JobManager, JobStatus
    from app.main import app

    mgr = JobManager()
    job = mgr.create("demo")
    # Terminal before the client subscribes, so the stream replays and closes
    # instead of blocking the test on a live job.
    job.progress.append({"event": "completed", "job_id": job.id})
    job.status = JobStatus.completed
    app.dependency_overrides[get_jobs] = lambda: mgr
    try:
        with TestClient(app) as c:
            with c.stream(
                "GET", f"/v1/jobs/{job.id}/stream", headers={"Accept-Encoding": "gzip"}
            ) as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream")
                assert "content-encoding" not in r.headers
                assert not b"".join(r.iter_raw()).startswith(b"\x1f\x8b")
    finally:
        app.dependency_overrides.pop(get_jobs, None)


def test_gzip_body_round_trips() -> None:
    """Decode the raw response body by hand to prove it is valid gzip."""
    app = FastAPI()
    app.add_middleware(CompressionMiddleware, minimum_size=1024)

    @app.get("/big")
    async def big() -> dict[str, str]:
        return {"markdown": BIG}

    with TestClient(app) as c:
        with c.stream("GET", "/big", headers={"Accept-Encoding": "gzip"}) as r:
            raw = b"".join(r.iter_raw())
    assert raw.startswith(b"\x1f\x8b")
    assert BIG.encode() in gzip.decompress(raw)
