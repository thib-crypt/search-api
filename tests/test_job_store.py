"""SQLite job persistence (§6.1).

Acceptance criterion from the plan: *a container restart preserves a finished
report*. A "restart" here is a fresh `JobManager` + `SqliteJobStore` opened
against the same file — same thing from the store's point of view.
"""

from __future__ import annotations

import asyncio

from app.jobs.manager import JobManager, JobStatus
from app.jobs.store import (
    RESTART_ERROR,
    InMemoryJobStore,
    SqliteJobStore,
    build_job_store,
)


def _manager(path, **kwargs) -> JobManager:
    return JobManager(store=SqliteJobStore(str(path)), **kwargs)


async def test_a_finished_report_survives_a_restart(tmp_path) -> None:
    db = tmp_path / "jobs.db"

    first = _manager(db, evict_interval_seconds=0)
    await first.start()
    job = await first.create("research")

    async def task(emit) -> dict:
        await emit({"stage": "searching"})
        await emit({"stage": "processed"})
        return {"report": "# Findings", "usage": {"total_cost_usd": 0.02}}

    first.submit(job, task)
    await first._tasks[job.id]
    await first.shutdown()

    # A new process, same file.
    second = _manager(db, evict_interval_seconds=0)
    await second.start()
    try:
        restored = second.get(job.id)
        assert restored is not None
        assert restored.status is JobStatus.completed
        assert restored.result == {"report": "# Findings", "usage": {"total_cost_usd": 0.02}}
        assert restored.type == "research"
        # And the SSE replay still works, from the persisted event log.
        stages = [e.get("stage") for e in restored.progress if "stage" in e]
        assert stages == ["searching", "processed"]
        events = [e async for e in second.stream(job.id)]
        assert any(e.get("event") == "completed" for e in events)
    finally:
        await second.shutdown()


async def test_a_job_left_running_by_a_dead_process_is_reconciled(tmp_path) -> None:
    """A background task cannot be resumed across a restart, so its record must
    not be left saying "running" forever."""
    db = tmp_path / "jobs.db"

    first = _manager(db, evict_interval_seconds=0)
    await first.start()
    job = await first.create("research")
    started = asyncio.Event()

    async def never_ends(emit) -> dict:
        started.set()
        await asyncio.sleep(60)
        return {}

    first.submit(job, never_ends)
    await started.wait()
    # Simulate a hard kill: drop the manager without letting the task finish.
    first._tasks[job.id].cancel()
    await asyncio.gather(first._tasks[job.id], return_exceptions=True)
    await first._store.update(
        _running(first.get(job.id))
    )  # put the row back in "running", as a kill -9 would leave it
    await first._store.close()

    second = _manager(db, evict_interval_seconds=0)
    await second.start()
    try:
        restored = second.get(job.id)
        assert restored is not None
        assert restored.status is JobStatus.failed
        assert restored.error == RESTART_ERROR
    finally:
        await second.shutdown()


def _running(job):
    job.status = JobStatus.running
    job.error = None
    return job


async def test_eviction_removes_the_persisted_rows_too(tmp_path) -> None:
    db = tmp_path / "jobs.db"

    mgr = _manager(db, retention_seconds=0.0, evict_interval_seconds=0)
    await mgr.start()

    async def task(emit) -> dict:
        await emit({"stage": "x"})
        return {}

    job = await mgr.create("demo")
    mgr.submit(job, task)
    await mgr._tasks[job.id]
    await mgr.create("demo")  # creating another job triggers the sweep
    assert mgr.get(job.id) is None
    await mgr.shutdown()

    revived = _manager(db, evict_interval_seconds=0)
    await revived.start()
    try:
        assert revived.get(job.id) is None  # not resurrected from disk either
    finally:
        await revived.shutdown()


async def test_retention_is_wall_clock_across_a_restart(tmp_path) -> None:
    """A job that finished long before the restart must not get a fresh lifetime."""
    db = tmp_path / "jobs.db"

    first = _manager(db, retention_seconds=3600.0, evict_interval_seconds=0)
    await first.start()
    job = await first.create("demo")

    async def task(emit) -> dict:
        return {}

    first.submit(job, task)
    await first._tasks[job.id]
    # Rewrite updated_at to look two hours old, as an aged database would.
    job.updated_at = "2000-01-01T00:00:00+00:00"
    await first._store.update(job)
    await first.shutdown()

    second = _manager(db, retention_seconds=3600.0, evict_interval_seconds=0)
    await second.start()
    try:
        assert second.get(job.id) is None  # evicted on the startup sweep
    finally:
        await second.shutdown()


async def test_persisted_events_are_bounded(tmp_path) -> None:
    """A 500-page crawl must not grow the database without bound."""
    db = tmp_path / "jobs.db"
    mgr = _manager(db, max_progress_events=10, evict_interval_seconds=0)
    await mgr.start()

    async def chatty(emit) -> dict:
        for i in range(100):
            await emit({"stage": "crawled", "n": i})
        return {}

    job = await mgr.create("site_crawl")
    mgr.submit(job, chatty)
    await mgr._tasks[job.id]
    await mgr.shutdown()

    revived = _manager(db, max_progress_events=10, evict_interval_seconds=0)
    await revived.start()
    try:
        restored = revived.get(job.id)
        assert restored is not None
        assert len(restored.progress) == 10
        assert restored.progress[-1]["event"] == "completed"  # newest kept
    finally:
        await revived.shutdown()


async def test_a_broken_store_never_fails_a_job(tmp_path) -> None:
    """Durability is best-effort by contract: a write failure is logged, not raised."""

    class ExplodingStore(InMemoryJobStore):
        async def update(self, job) -> None:
            raise OSError("disk full")

        async def append_event(self, job_id, payload, cap) -> None:
            raise OSError("disk full")

    mgr = JobManager(store=ExplodingStore(), evict_interval_seconds=0)
    await mgr.start()
    job = await mgr.create("demo")

    async def task(emit) -> dict:
        await emit({"stage": "a"})
        return {"ok": True}

    mgr.submit(job, task)
    await mgr._tasks[job.id]

    # The store is what broke, not the job.
    assert job.status is JobStatus.completed
    assert job.result == {"ok": True}


def test_backend_selection() -> None:
    assert isinstance(build_job_store("memory", "unused"), InMemoryJobStore)
    assert isinstance(build_job_store("sqlite", "/tmp/x.db"), SqliteJobStore)


async def test_vacuum_is_a_no_op_when_there_is_nothing_to_reclaim(tmp_path) -> None:
    store = SqliteJobStore(str(tmp_path / "nested" / "dir" / "jobs.db"))
    await store.start()  # the directory did not exist
    try:
        await store.maintain()
    finally:
        await store.close()
