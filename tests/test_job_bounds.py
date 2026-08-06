"""Memory bounds on the job manager (§2.6, §2.7)."""

from __future__ import annotations

import asyncio

from app.api.middleware import _HITS, _purge, reset_rate_limiter
from app.jobs.manager import JobManager, JobStatus


async def test_progress_history_is_bounded() -> None:
    mgr = JobManager(max_progress_events=10)
    job = mgr.create("demo")

    async def task(emit) -> dict:
        for i in range(100):  # a 500-page site crawl emits one event per page
            await emit({"stage": "crawled", "n": i})
        return {}

    mgr.submit(job, task)
    await mgr._tasks[job.id]

    assert len(job.progress) == 10
    # The newest events survive, the oldest are dropped.
    assert job.progress[-1]["event"] == "completed"
    assert [e["n"] for e in job.progress if "n" in e] == list(range(91, 100))


async def test_slow_subscriber_queue_is_bounded_and_keeps_the_sentinel() -> None:
    mgr = JobManager(subscriber_queue_size=5)
    job = mgr.create("demo")
    queue: asyncio.Queue = asyncio.Queue(maxsize=5)
    mgr._subscribers[job.id].add(queue)

    for i in range(50):  # a subscriber that never reads
        mgr._fanout(job.id, {"event": "progress", "n": i})

    assert queue.qsize() == 5
    drained = [queue.get_nowait() for _ in range(5)]
    assert [e["n"] for e in drained] == [45, 46, 47, 48, 49]  # oldest dropped, newest kept


async def test_stream_drains_events_published_during_replay() -> None:
    """Replaying a finished job must not swallow the events that close it out."""
    mgr = JobManager()
    job = mgr.create("demo")
    for i in range(3):
        await mgr._publish(job, {"event": "progress", "n": i})

    events = []
    stream = mgr.stream(job.id)
    # Consume the first replayed event, then finish the job mid-replay: the
    # remaining events land in the subscriber queue, not in the replay snapshot.
    events.append(await anext(stream))
    job.status = JobStatus.completed
    await mgr._publish(job, {"event": "completed"})

    async for event in stream:
        events.append(event)

    assert [e.get("n") for e in events if "n" in e] == [0, 1, 2]
    assert events[-1] == {"event": "completed"}


async def test_background_evictor_drops_aged_jobs_without_new_activity() -> None:
    """Eviction used to require creating another job; a quiet instance never freed."""
    mgr = JobManager(retention_seconds=0.0, evict_interval_seconds=0.01)
    mgr.start()

    async def task(emit) -> dict:
        return {"big": "result"}

    job = mgr.create("demo")
    mgr.submit(job, task)
    await mgr._tasks[job.id]
    assert mgr.get(job.id) is not None

    for _ in range(50):  # no further job creation — only the sweeper can free it
        await asyncio.sleep(0.01)
        if mgr.get(job.id) is None:
            break

    assert mgr.get(job.id) is None
    await mgr.shutdown()


async def test_shutdown_stops_the_evictor() -> None:
    mgr = JobManager(evict_interval_seconds=0.01)
    mgr.start()
    evictor = mgr._evictor
    assert evictor is not None
    await mgr.shutdown()
    assert evictor.done()
    assert mgr._evictor is None


def test_rate_limiter_table_is_purged_and_capped() -> None:
    reset_rate_limiter()
    from collections import deque

    now = 1000.0
    for i in range(50):
        _HITS[f"ip-{i}"] = deque([now - 120.0])  # last hit well outside the window
    _HITS["fresh"] = deque([now])

    _purge(now, max_clients=10_000)

    assert list(_HITS) == ["fresh"]  # stale windows dropped
    reset_rate_limiter()


def test_rate_limiter_evicts_coldest_clients_over_the_cap() -> None:
    reset_rate_limiter()
    from collections import deque

    now = 1000.0
    for i in range(20):
        _HITS[f"ip-{i}"] = deque([now])

    _purge(now, max_clients=5)

    assert len(_HITS) == 5
    assert list(_HITS) == [f"ip-{i}" for i in range(15, 20)]  # newest retained
    reset_rate_limiter()
