"""In-process async job manager with progress pub/sub for SSE.

Long-running work (deep research, whole-site crawls) runs as a background
``asyncio.Task``. Progress events are appended to the job and fanned out to any
live SSE subscribers. The storage is in-memory behind a small surface, so it can
be swapped for Redis later without touching the routes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

Emit = Callable[[dict[str, Any]], Awaitable[None]]
JobTask = Callable[[Emit], Awaitable[dict[str, Any]]]

# Pushed onto subscriber queues to signal end-of-stream.
_SENTINEL: dict[str, Any] = {"event": "__end__"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    progress: list[dict[str, Any]] = field(default_factory=list)
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
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def shutdown(self) -> None:
        """Cancel all in-flight jobs and wait for them to unwind."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def create(self, job_type: str) -> Job:
        job = Job(id=uuid4().hex, type=job_type)
        self._jobs[job.id] = job
        self._subscribers[job.id] = set()
        return job

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
            self._fanout(job.id, _SENTINEL)

    async def _publish(self, job: Job, event: dict[str, Any]) -> None:
        job.updated_at = _now()
        job.progress.append(event)
        self._fanout(job.id, event)

    def _fanout(self, job_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(job_id, ())):
            queue.put_nowait(event)

    async def stream(self, job_id: str):
        """Yield events for a job: a replay of progress so far, then live updates."""
        job = self._jobs.get(job_id)
        if job is None:
            return

        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[job_id].add(queue)
        try:
            seen = len(job.progress)
            for event in job.progress[:seen]:
                yield event
            if job.terminal:
                return
            while True:
                event = await queue.get()
                if event is _SENTINEL:
                    return
                yield event
        finally:
            self._subscribers[job_id].discard(queue)
