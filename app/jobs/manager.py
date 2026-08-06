"""In-process async job manager with progress pub/sub for SSE.

Long-running work (deep research, whole-site crawls) runs as a background
``asyncio.Task``. Progress events are appended to the job and fanned out to any
live SSE subscribers.

Live jobs are held in a plain dict, which is the read path: one worker, one
process, so `get()` stays synchronous and allocation-free. Durability (§6.1) is
a **write-through mirror** onto a `JobStore` — SQLite in production, a no-op in
tests — hydrated back into the dict at startup. A store that fails is logged and
ignored; see `app.jobs.store` for why.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:  # avoids a cycle: the store imports Job/JobStatus from here
    from app.jobs.store import JobStore

logger = logging.getLogger("unified_search_api.jobs")

Emit = Callable[[dict[str, Any]], Awaitable[None]]
JobTask = Callable[[Emit], Awaitable[dict[str, Any]]]
# Every subscriber queue and every published event carries the same payload shape.
EventQueue = asyncio.Queue[dict[str, Any]]

# Pushed onto subscriber queues to signal end-of-stream.
_SENTINEL: dict[str, Any] = {"event": "__end__"}

# A site crawl emits one event per page, so both the retained history and each
# subscriber's buffer are capped: a 500-page crawl must not grow without bound,
# and a slow SSE client must not be able to make the server buffer for it.
_DEFAULT_MAX_PROGRESS = 500
_DEFAULT_QUEUE_SIZE = 1000


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _age_seconds(timestamp: str) -> float:
    """Seconds since an ISO timestamp, clamped at 0. Unparseable reads as brand new."""
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - parsed).total_seconds(), 0.0)


def _offer(queue: EventQueue, event: dict[str, Any]) -> None:
    """Enqueue ``event``, dropping the oldest entries if the subscriber is behind.

    An unbounded queue lets one slow SSE reader pin the memory of a whole crawl.
    Dropping the oldest progress event is the right trade: it is stale by then.
    The end-of-stream sentinel is never dropped — a subscriber that missed it
    would wait forever.
    """
    while True:
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - single-threaded loop
                return


class JobStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


@dataclass
class Job:
    id: str
    type: str
    status: JobStatus = JobStatus.pending
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    # Bounded ring: the oldest progress events are dropped once it is full.
    progress: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_DEFAULT_MAX_PROGRESS)
    )
    result: dict[str, Any] | None = None
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in (JobStatus.completed, JobStatus.failed)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": list(self.progress),
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(
        self,
        *,
        retention_seconds: float = 3600.0,
        evict_interval_seconds: float = 300.0,
        max_progress_events: int = _DEFAULT_MAX_PROGRESS,
        subscriber_queue_size: int = _DEFAULT_QUEUE_SIZE,
        store: JobStore | None = None,
        vacuum_interval_seconds: float = 0.0,
    ) -> None:
        self._jobs: dict[str, Job] = {}
        self._subscribers: dict[str, set[EventQueue]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Without eviction, finished jobs (and their results) would pin memory for
        # the process lifetime. Terminal jobs are dropped once they age past this.
        self._retention = retention_seconds
        self._finished_at: dict[str, float] = {}
        self._max_progress = max(max_progress_events, 1)
        self._queue_size = max(subscriber_queue_size, 1)
        self._evict_interval = evict_interval_seconds
        self._evictor: asyncio.Task[None] | None = None
        # Defaults to the no-op store so a bare `JobManager()` — which is what
        # every test builds — never touches a disk. Production passes a real one.
        if store is None:
            from app.jobs.store import InMemoryJobStore

            store = InMemoryJobStore()
        self._store = store
        self._vacuum_interval = vacuum_interval_seconds
        self._last_vacuum = time.monotonic()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def active_counts(self) -> dict[str, int]:
        """Non-terminal jobs per type, recomputed on demand (§7.2).

        Sampled by `GET /metrics` rather than maintained as a counter through
        every state transition: the table is small and a derived count cannot
        drift out of sync with reality, whereas a hand-maintained one can.
        """
        counts: dict[str, int] = {}
        for job in self._jobs.values():
            if not job.terminal:
                counts[job.type] = counts.get(job.type, 0) + 1
        return counts

    async def _persist(self, awaitable: Awaitable[Any], what: str) -> None:
        """Await a store operation, swallowing its failures.

        The store is a durability mirror, not the source of truth (see
        `app.jobs.store`): a full disk, a locked database or a store
        implementation that raises must cost the deployment its restart-survival,
        never the job it is in the middle of running.
        """
        try:
            await awaitable
        except Exception:  # noqa: BLE001 - degraded durability beats a failed job
            logger.exception("Job store could not %s", what)

    async def start(self) -> None:
        """Open the store, hydrate whatever survived, and start the sweeper.

        Eviction used to happen only when a new job was created, so an instance
        that went quiet kept every finished report — up to 500 pages of markdown
        each — for as long as the process lived.
        """
        await self._persist(self._store.start(), "start")
        try:
            restored = await self._store.load_all(self._max_progress)
        except Exception:  # noqa: BLE001 - an unreadable mirror starts empty
            logger.exception("Job store could not be read; starting with no jobs")
            restored = []
        for job in restored:
            self._jobs[job.id] = job
            self._subscribers.setdefault(job.id, set())
            if job.terminal:
                # Retention is wall-clock, so a job that finished long before the
                # restart is already stale and must not get a fresh hour of life.
                self._finished_at[job.id] = time.monotonic() - _age_seconds(job.updated_at)
        if self._jobs:
            logger.info("Restored %d job(s) from the job store", len(self._jobs))
        await self._evict()

        if self._evict_interval > 0 and self._evictor is None:
            self._evictor = asyncio.create_task(self._evict_loop())

    async def _evict_loop(self) -> None:
        while True:
            await asyncio.sleep(self._evict_interval)
            try:
                await self._evict()
                now = time.monotonic()
                if self._vacuum_interval > 0 and now - self._last_vacuum >= self._vacuum_interval:
                    self._last_vacuum = now
                    await self._persist(self._store.maintain(), "run its periodic maintenance")
            except Exception:  # noqa: BLE001 - the sweeper must never die
                logger.exception("Job eviction sweep failed")

    async def shutdown(self) -> None:
        """Cancel the sweeper and all in-flight jobs, then wait for them to unwind."""
        if self._evictor is not None:
            self._evictor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._evictor
            self._evictor = None

        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._persist(self._store.close(), "close")

    async def create(self, job_type: str) -> Job:
        await self._evict()
        job = Job(id=uuid4().hex, type=job_type, progress=deque(maxlen=self._max_progress))
        self._jobs[job.id] = job
        self._subscribers[job.id] = set()
        await self._persist(self._store.create(job), "persist a new job")
        return job

    async def _evict(self) -> None:
        """Drop terminal jobs that have aged past the retention window."""
        if not self._finished_at:
            return
        cutoff = time.monotonic() - self._retention
        stale = [jid for jid, ts in self._finished_at.items() if ts <= cutoff]
        for jid in stale:
            self._jobs.pop(jid, None)
            self._subscribers.pop(jid, None)
            self._tasks.pop(jid, None)
            self._finished_at.pop(jid, None)
        if stale:
            await self._persist(self._store.delete(stale), "evict aged-out jobs")

    def cancel(self, job_id: str) -> bool:
        """Cancel an in-flight job. Returns False if the job is unknown or already terminal."""
        job = self._jobs.get(job_id)
        if job is None or job.terminal:
            return False
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        return True

    def submit(self, job: Job, task: JobTask) -> None:
        self._tasks[job.id] = asyncio.create_task(self._run(job, task))

    async def _run(self, job: Job, task: JobTask) -> None:
        job.status = JobStatus.running
        await self._publish(job, {"event": "status", "status": JobStatus.running.value})

        async def emit(data: dict[str, Any]) -> None:
            await self._publish(job, {"event": "progress", **data})

        try:
            job.result = await task(emit)
            job.status = JobStatus.completed
            await self._publish(job, {"event": "completed"})
        except asyncio.CancelledError:
            job.status = JobStatus.failed
            job.error = "cancelled"
            await self._publish(job, {"event": "failed", "error": "cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001 - surface any task failure to the client
            job.status = JobStatus.failed
            job.error = str(exc)
            await self._publish(job, {"event": "failed", "error": str(exc)})
        finally:
            self._finished_at[job.id] = time.monotonic()
            self._fanout(job.id, _SENTINEL)

    async def _publish(self, job: Job, event: dict[str, Any]) -> None:
        job.updated_at = _now()
        job.progress.append(event)
        self._fanout(job.id, event)
        # Persisted after the fanout: a live subscriber must never wait on a disk
        # write for its next progress event.
        await self._persist(
            self._store.append_event(job.id, event, self._max_progress), "persist a progress event"
        )
        await self._persist(self._store.update(job), "persist a job update")

    def _fanout(self, job_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(job_id, ())):
            _offer(queue, event)

    async def stream(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield events for a job: a replay of progress so far, then live updates."""
        job = self._jobs.get(job_id)
        if job is None:
            return

        queue: EventQueue = asyncio.Queue(maxsize=self._queue_size)
        subscribers = self._subscribers.setdefault(job_id, set())
        subscribers.add(queue)
        try:
            # Subscribing and snapshotting happen without an await in between, so
            # every event is either in the snapshot or in the queue — never both,
            # never neither.
            for event in list(job.progress):
                yield event

            if job.terminal:
                # Events published *during* the replay above are sitting in the
                # queue. Returning here without draining loses the final ones —
                # typically `completed`, the only event the client is waiting for.
                while True:
                    try:
                        event = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    if event is _SENTINEL:
                        return
                    yield event

            while True:
                event = await queue.get()
                if event is _SENTINEL:
                    return
                yield event
        finally:
            self._subscribers.get(job_id, set()).discard(queue)
