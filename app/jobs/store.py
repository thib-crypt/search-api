"""Durable storage behind `JobManager` (§6.1).

`JobManager` has always kept every live job in a plain dict, and it still does:
one process, one worker, so the dict *is* the read path — sync, allocation-free,
and on the hot path of every SSE poll. What it lacked was durability. Restarting
the container dropped finished reports on the floor and turned a client's
`GET /v1/jobs/{id}` into a 404.

A `JobStore` is therefore a **write-through mirror**, not the read path:

* every mutation is also written to the store, best-effort;
* at startup `load_all()` hydrates the dict back from it;
* a store failure is logged and ignored — losing durability is bad, failing a
  ten-minute research run because a disk hiccuped is worse.

Two implementations: `InMemoryJobStore` (what the process already does, kept so
the manager has exactly one code path — and the default in tests, which must
never touch disk) and `SqliteJobStore`.

What is deliberately *not* here: the SSE pub/sub. Subscribers are always in the
same process as the publisher, so fanning out through storage would buy nothing.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import Any, Protocol

from app.core.db import connect
from app.jobs.manager import Job, JobStatus

logger = logging.getLogger("unified_search_api.jobs.store")

# How far the persisted event log may run past the in-memory cap before it is
# pruned back. Pruning on every append would cost a DELETE per progress event of
# a 500-page crawl; a little slack makes it amortized.
_PRUNE_SLACK = 1.5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    result_json TEXT,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS job_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_events_by_job ON job_events (job_id, seq);
CREATE TABLE IF NOT EXISTS cost_ledger (
    label    TEXT NOT NULL,
    day      TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (label, day)
);
"""

# What a job left running by a killed process is marked with. A background task
# cannot be resumed across a restart — only its record can be reconciled, and
# leaving it "running" forever is the one outcome a client can never recover from.
RESTART_ERROR = "process restarted mid-run"


class JobStore(Protocol):
    """The storage operations `JobManager` actually performs. Nothing more."""

    async def start(self) -> None:
        """Open resources and reconcile anything a previous process left behind."""

    async def close(self) -> None: ...

    async def load_all(self, max_progress: int) -> list[Job]:
        """Every persisted job, progress included, newest events last."""

    async def create(self, job: Job) -> None: ...

    async def update(self, job: Job) -> None:
        """Persist status / result / error / updated_at."""

    async def append_event(self, job_id: str, payload: dict[str, Any], cap: int) -> None: ...

    async def delete(self, job_ids: list[str]) -> None: ...

    async def maintain(self) -> None:
        """Periodic housekeeping (VACUUM). A no-op for stores that need none."""


class InMemoryJobStore:
    """The process's own dicts. Loses everything on restart, by design."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self._jobs.clear()

    async def load_all(self, max_progress: int) -> list[Job]:
        return list(self._jobs.values())

    async def create(self, job: Job) -> None:
        self._jobs[job.id] = job

    async def update(self, job: Job) -> None:
        # The manager mutates the same object, so there is nothing to copy back.
        self._jobs[job.id] = job

    async def append_event(self, job_id: str, payload: dict[str, Any], cap: int) -> None:
        return None

    async def delete(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            self._jobs.pop(job_id, None)

    async def maintain(self) -> None:
        return None


class SqliteJobStore:
    """Write-through mirror onto a single SQLite file.

    Every method swallows its own failures: this class exists to make a restart
    survivable, and it must never be the reason a job fails.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: Any = None
        # Rows written per job since the last prune, to amortize the trimming.
        self._written: dict[str, int] = {}

    async def start(self) -> None:
        self._conn = await connect(self.db_path)
        await self._conn.executescript(_SCHEMA)
        # A job in a non-terminal state can only be a leftover: the process that
        # owned its task is gone.
        cursor = await self._conn.execute(
            "UPDATE jobs SET status = ?, error = ? WHERE status IN (?, ?)",
            (JobStatus.failed.value, RESTART_ERROR, JobStatus.pending.value, "running"),
        )
        await self._conn.commit()
        if cursor.rowcount:
            logger.warning(
                "Reconciled %d job(s) left running by a previous process", cursor.rowcount
            )

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.exception("Closing the job database failed")
            self._conn = None

    async def load_all(self, max_progress: int) -> list[Job]:
        if self._conn is None:
            return []
        try:
            jobs: dict[str, Job] = {}
            async with self._conn.execute(
                "SELECT id, type, status, created_at, updated_at, result_json, error FROM jobs"
            ) as cursor:
                async for row in cursor:
                    jobs[row["id"]] = Job(
                        id=row["id"],
                        type=row["type"],
                        status=JobStatus(row["status"]),
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        progress=deque(maxlen=max(max_progress, 1)),
                        result=_loads(row["result_json"]),
                        error=row["error"],
                    )
            # Appended in seq order into a bounded deque, so an over-long history
            # trims to its newest events exactly as it would have in memory.
            async with self._conn.execute(
                "SELECT job_id, payload_json FROM job_events ORDER BY seq"
            ) as cursor:
                async for row in cursor:
                    job = jobs.get(row["job_id"])
                    payload = _loads(row["payload_json"])
                    if job is not None and payload is not None:
                        job.progress.append(payload)
            return list(jobs.values())
        except Exception:  # noqa: BLE001 - an unreadable mirror starts empty
            logger.exception("Loading persisted jobs failed; starting with none")
            return []

    async def create(self, job: Job) -> None:
        await self._run(
            "INSERT OR REPLACE INTO jobs "
            "(id, type, status, created_at, updated_at, result_json, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                job.id,
                job.type,
                job.status.value,
                job.created_at,
                job.updated_at,
                _dumps(job.result),
                job.error,
            ),
            what="persist a new job",
        )

    async def update(self, job: Job) -> None:
        await self._run(
            "UPDATE jobs SET status = ?, updated_at = ?, result_json = ?, error = ? WHERE id = ?",
            (job.status.value, job.updated_at, _dumps(job.result), job.error, job.id),
            what="persist a job update",
        )

    async def append_event(self, job_id: str, payload: dict[str, Any], cap: int) -> None:
        if self._conn is None:
            return
        encoded = _dumps(payload)
        if encoded is None:
            return
        await self._run(
            "INSERT INTO job_events (job_id, payload_json) VALUES (?, ?)",
            (job_id, encoded),
            what="persist a progress event",
        )
        cap = max(cap, 1)
        written = self._written.get(job_id, 0) + 1
        if written <= cap * _PRUNE_SLACK:
            self._written[job_id] = written
            return
        self._written[job_id] = cap
        await self._run(
            "DELETE FROM job_events WHERE job_id = ? AND seq <= "
            "(SELECT seq FROM job_events WHERE job_id = ? ORDER BY seq DESC LIMIT 1 OFFSET ?)",
            (job_id, job_id, cap - 1),
            what="trim a job's event log",
        )

    async def delete(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        placeholders = ",".join("?" * len(job_ids))
        await self._run(
            f"DELETE FROM job_events WHERE job_id IN ({placeholders})",
            tuple(job_ids),
            what="evict job events",
        )
        await self._run(
            f"DELETE FROM jobs WHERE id IN ({placeholders})",
            tuple(job_ids),
            what="evict jobs",
        )
        for job_id in job_ids:
            self._written.pop(job_id, None)

    async def maintain(self) -> None:
        """Reclaim the space evicted jobs left behind.

        Run on its own coarse interval (`JOB_VACUUM_INTERVAL_SECONDS`, a day by
        default) rather than with the evict sweep: VACUUM rewrites the entire
        file, which is not something to do every five minutes.
        """
        if self._conn is None:
            return
        try:
            await self._conn.execute("VACUUM")
            await self._conn.commit()
        except Exception:  # noqa: BLE001 - housekeeping, never fatal
            logger.exception("VACUUM of the job database failed")

    async def _run(self, sql: str, params: tuple[Any, ...], *, what: str) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute(sql, params)
            await self._conn.commit()
        except Exception:  # noqa: BLE001 - durability is best-effort by contract
            logger.exception("Could not %s", what)


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        logger.warning("Job payload is not JSON-serializable; not persisted")
        return None


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def build_job_store(backend: str, db_path: str) -> JobStore:
    """Pick an implementation from config. Called once, at application startup."""
    if backend == "memory":
        return InMemoryJobStore()
    if backend != "sqlite":  # pragma: no cover - Literal-typed in Settings
        logger.warning("Unknown JOB_BACKEND=%r; falling back to memory", backend)
        return InMemoryJobStore()
    return SqliteJobStore(db_path)
