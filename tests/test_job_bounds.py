"""Memory bounds on the job manager (§2.6, §2.7)."""

from __future__ import annotations

import asyncio

from app.api.middleware import _HITS, _purge, reset_rate_limiter
from app.jobs.manager import JobManager, JobStatus


async def test_progress_history_is_bounded() -> None:
    mgr = JobManager(max_progress_events=10)
    job = await mgr.create("demo")

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
    job = await mgr.create("demo")
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
    job = await mgr.create("demo")
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
    await mgr.start()

    async def task(emit) -> dict:
        return {"big": "result"}

    job = await mgr.create("demo")
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
    await mgr.start()
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
        _HITS[f"ip-{i}"] = deque([(now - 120.0, 1)])  # last hit well outside the window
    _HITS["fresh"] = deque([(now, 1)])

    _purge(now, max_clients=10_000)

    assert list(_HITS) == ["fresh"]  # stale windows dropped
    reset_rate_limiter()


def test_rate_limiter_evicts_coldest_clients_over_the_cap() -> None:
    reset_rate_limiter()
    from collections import deque

    now = 1000.0
    for i in range(20):
        _HITS[f"ip-{i}"] = deque([(now, 1)])

    _purge(now, max_clients=5)

    assert len(_HITS) == 5
    assert list(_HITS) == [f"ip-{i}" for i in range(15, 20)]  # newest retained
    reset_rate_limiter()


async def test_shutdown_cancels_in_flight_jobs_and_returns() -> None:
    """The SIGTERM path: a container stopping mid-research must not hang.

    `test_shutdown_stops_the_evictor` only ever runs with an empty task table, so
    the cancel-all-and-await branch — the one that actually runs on every
    redeploy with a ten-minute run in flight — was never executed. Cancelling
    drives `_run`'s `CancelledError` branch, which still awaits a publish and two
    store writes on the way out; if any of that deadlocked, `shutdown()` would
    never return.
    """
    mgr = JobManager(evict_interval_seconds=0.01)
    await mgr.start()
    job = await mgr.create("research")
    started = asyncio.Event()

    async def never_finishes(emit) -> dict:
        await emit({"stage": "working"})
        started.set()
        await asyncio.sleep(600)
        return {}  # pragma: no cover - cancelled long before this

    mgr.submit(job, never_finishes)
    await started.wait()
    assert job.status is JobStatus.running

    await asyncio.wait_for(mgr.shutdown(), timeout=2.0)

    assert job.status is JobStatus.failed
    assert job.error == "cancelled"
    assert mgr._tasks[job.id].done()


async def test_fanout_survives_a_subscriber_leaving_mid_stream() -> None:
    """`_fanout` snapshots its subscriber set so one reader can leave while it runs.

    Every other stream test uses exactly one subscriber, so the interleaving of
    `stream()`'s `finally` discard with an in-progress fanout was unasserted.
    """
    mgr = JobManager()
    job = await mgr.create("demo")

    async def quitter() -> None:
        # Closed explicitly rather than by abandoning the `async for`: an
        # abandoned async generator is only finalised at GC, so its `finally`
        # would not run inside this test. `aclose()` is what a disconnected SSE
        # client's response actually triggers.
        gen = mgr.stream(job.id)
        await gen.__anext__()  # subscribe, then take one event
        await gen.aclose()  # runs the `finally`, discarding its queue mid-fanout

    async def reader() -> list[dict]:
        return [event async for event in mgr.stream(job.id)]

    async def task(emit) -> dict:
        for i in range(20):
            await emit({"n": i})
        return {}

    readers = [asyncio.create_task(reader()) for _ in range(2)]
    await asyncio.sleep(0)  # let both subscribe before anything is published
    leaving = asyncio.create_task(quitter())
    mgr.submit(job, task)

    received = await asyncio.gather(*readers)
    await leaving

    for events in received:
        assert [e["n"] for e in events if "n" in e] == list(range(20))
        assert events[-1]["event"] == "completed"
    # Every subscriber unsubscribed itself on the way out.
    assert mgr._subscribers[job.id] == set()
