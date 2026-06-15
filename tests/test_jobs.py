import pytest
from starlette.testclient import TestClient

from app.api.deps import get_crawler, get_jobs, get_llm, get_searxng
from app.jobs.manager import JobManager, JobStatus
from app.main import app
from app.models.crawl import CrawledPage
from tests.test_research import FakeCrawler, FakeLLM, FakeSearxng


@pytest.mark.asyncio
async def test_job_runs_and_records_result() -> None:
    mgr = JobManager()
    job = mgr.create("demo")

    async def task(emit) -> dict:
        await emit({"stage": "a"})
        await emit({"stage": "b"})
        return {"ok": True}

    mgr.submit(job, task)
    await mgr._tasks[job.id]

    assert job.status is JobStatus.completed
    assert job.result == {"ok": True}
    stages = [e.get("stage") for e in job.progress if "stage" in e]
    assert stages == ["a", "b"]


@pytest.mark.asyncio
async def test_job_records_failure() -> None:
    mgr = JobManager()
    job = mgr.create("demo")

    async def task(emit) -> dict:
        raise ValueError("boom")

    mgr.submit(job, task)
    await mgr._tasks[job.id]

    assert job.status is JobStatus.failed
    assert job.error == "boom"
    assert any(e.get("event") == "failed" for e in job.progress)


@pytest.mark.asyncio
async def test_finished_jobs_are_evicted_after_retention() -> None:
    mgr = JobManager(retention_seconds=0.0)  # evict as soon as a job is terminal

    async def task(emit) -> dict:
        return {}

    first = mgr.create("demo")
    mgr.submit(first, task)
    await mgr._tasks[first.id]
    assert first.status is JobStatus.completed

    # Creating another job triggers eviction of the now-stale finished one.
    mgr.create("demo")
    assert mgr.get(first.id) is None


@pytest.mark.asyncio
async def test_stream_replays_progress_after_completion() -> None:
    mgr = JobManager()
    job = mgr.create("demo")

    async def task(emit) -> dict:
        await emit({"stage": "only"})
        return {}

    mgr.submit(job, task)
    await mgr._tasks[job.id]

    events = [e async for e in mgr.stream(job.id)]
    assert {"stage": "only", "event": "progress"} in [
        {k: v for k, v in e.items()} for e in events if e.get("stage") == "only"
    ]
    assert any(e.get("event") == "completed" for e in events)


class SiteFakeCrawler:
    async def crawl_site(self, req, emit=None) -> list[CrawledPage]:
        return [CrawledPage(url=str(req.url), success=True, markdown="page")]


def _override_research(jobs: JobManager):
    app.dependency_overrides[get_searxng] = lambda: FakeSearxng()
    app.dependency_overrides[get_crawler] = lambda: FakeCrawler()
    app.dependency_overrides[get_llm] = lambda: FakeLLM(feedback_questions=[])
    app.dependency_overrides[get_jobs] = lambda: jobs


def test_research_background_returns_job_ref() -> None:
    jobs = JobManager()
    _override_research(jobs)
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research",
                json={"query": "x", "background": True, "depth": 1, "breadth": 2},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "research"
        assert body["stream_url"] == f"/v1/jobs/{body['job_id']}/stream"
        assert jobs.get(body["job_id"]) is not None
    finally:
        app.dependency_overrides.clear()


def test_site_crawl_submits_job() -> None:
    jobs = JobManager()
    app.dependency_overrides[get_crawler] = lambda: SiteFakeCrawler()
    app.dependency_overrides[get_jobs] = lambda: jobs
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/crawl/site", json={"url": "https://example.com", "max_depth": 1}
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["type"] == "site_crawl"
        assert jobs.get(body["job_id"]) is not None
    finally:
        app.dependency_overrides.clear()


def test_get_unknown_job_404() -> None:
    with TestClient(app) as client:
        resp = client.get("/v1/jobs/does-not-exist")
    assert resp.status_code == 404


def test_cancel_unknown_job_404() -> None:
    with TestClient(app) as client:
        resp = client.delete("/v1/jobs/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cancel_running_job_returns_202() -> None:
    import asyncio

    mgr = JobManager()
    started = asyncio.Event()

    async def long_task(emit) -> dict:
        started.set()
        await asyncio.sleep(60)
        return {}

    job = mgr.create("demo")
    mgr.submit(job, long_task)
    await started.wait()  # ensure the coroutine is actually running before cancelling

    assert mgr.cancel(job.id) is True
    with pytest.raises(asyncio.CancelledError):
        await mgr._tasks[job.id]
    assert job.status is JobStatus.failed
    assert job.error == "cancelled"


@pytest.mark.asyncio
async def test_cancel_terminal_job_returns_false() -> None:
    mgr = JobManager()

    async def task(emit) -> dict:
        return {}

    job = mgr.create("demo")
    mgr.submit(job, task)
    await mgr._tasks[job.id]

    assert mgr.cancel(job.id) is False
