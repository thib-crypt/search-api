"""Long operations: the bridge from MCP tool calls onto the `JobManager` (§4).

`deep_research` and `crawl_site` take minutes. A synchronous tool call that
blocks for eight of them is a call most clients time out, so neither tool returns
its result: both start a job and hand back its id.

**No engine is added here.** `JobManager` already does creation, background
execution, progress via `emit`, terminal status, cancellation, SQLite persistence
and restart reconciliation. This module only translates between it and the MCP
surface — and, crucially, the id it returns *is* the job id, so a run started by
an agent stays visible at `GET /v1/jobs/{id}`, streams over its SSE channel, and
is cancelled by `DELETE /v1/jobs/{id}` like any other.

### On MCP tasks

§4 of the plan targeted the `tasks/*` primitive of the 2025-11-25 revision, with
polling as the mandatory fallback for clients that had not negotiated it. That
plan predates the SDK that actually shipped: `mcp` 2.0 speaks 2026-07-28, which
**removed** tasks — `mcp.types` still defines `Task` and `CreateTaskResult`, but
`tasks/get` is not in the SDK's method table and no server-side handler exists
for it. Serving them would mean hand-rolling protocol handlers for a revision the
runtime has moved past, and clients would not look for them anyway.

So the fallback path of §4 is the path, and it is the one §4 already specified in
full: create the job, return `{job_id, status, poll_with}` immediately with an
explicit instruction to poll `job_status`, and deliver the result there. One code
path, no capability negotiation, and nothing about the job differs from one the
HTTP API started.
"""

from __future__ import annotations

import logging
from typing import Any

from app.jobs.manager import Emit, Job, JobManager, JobTask
from app.mcp.budget import job_uri

logger = logging.getLogger("unified_search_api.mcp")

# What `job_status` tells an agent to wait between polls, per job type. Deep
# research answers in minutes and site crawls in tens of seconds; polling either
# one every second is a rate-limit bill for no information.
POLL_SECONDS: dict[str, int] = {"research": 20, "site_crawl": 10}
DEFAULT_POLL_SECONDS = 15


async def launch(jobs: JobManager, job_type: str, task: JobTask) -> Job:
    """Create and submit a job. Same two calls the HTTP routes make."""
    job = await jobs.create(job_type)
    jobs.submit(job, task)
    return job


def submitted_payload(job: Job, *, tool: str, note: str = "") -> dict[str, Any]:
    """The structured result of a tool that started a long run.

    Deliberately verbose about *what to do next*. An agent that receives an
    opaque identifier will either poll it every 200 ms or forget about it; one
    that is told the tool name, the argument and the interval does neither.
    """
    poll = POLL_SECONDS.get(job.type, DEFAULT_POLL_SECONDS)
    payload: dict[str, Any] = {
        "job_id": job.id,
        "type": job.type,
        "status": job.status.value,
        "started_by": tool,
        "poll_with": {
            "tool": "job_status",
            "arguments": {"job_id": job.id},
            "wait_seconds_between_polls": poll,
        },
        "resource": job_uri(job.id),
        # The same job over HTTP, for a caller that has both surfaces.
        "http_status_url": f"/v1/jobs/{job.id}",
        "http_stream_url": f"/v1/jobs/{job.id}/stream",
    }
    if note:
        payload["note"] = note
    return payload


def submitted_text(job: Job, *, tool: str, note: str = "") -> str:
    poll = POLL_SECONDS.get(job.type, DEFAULT_POLL_SECONDS)
    lines = [
        f"Started a {job.type} job: {job.id}",
        "",
        f"It is running in the background and will take minutes, not seconds. "
        f'Call job_status(job_id="{job.id}") to collect the result — '
        f"waiting about {poll}s between polls is enough; polling faster only "
        f"spends rate-limit budget.",
    ]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def progress_tail(job: Job, limit: int = 5) -> list[dict[str, Any]]:
    """The last few progress events, for a job that has not finished.

    Bounded because the trail is unbounded: a 500-page crawl emits one event per
    page, and an agent polling a running job needs to know *where* it is, not to
    receive the whole history on every poll.
    """
    events = [e for e in job.progress if e.get("event") == "progress"]
    # Report tokens are a streaming artefact of the HTTP SSE channel; an agent
    # wants the report, not to watch it being typed (§4).
    events = [e for e in events if e.get("stage") != "report_token"]
    return events[-limit:]


def research_emitter(emit: Emit) -> Any:
    """A `ResearchProgress` hook that writes the engine's stages onto a job.

    Identical to the one `POST /v1/research` installs, so a run started from MCP
    produces the same event stream on `/v1/jobs/{id}/stream` as one started over
    HTTP. Token-level streaming is not wired: see `progress_tail`.
    """

    async def on_progress(progress: Any) -> None:
        await emit(
            {
                "stage": progress.stage,
                "depth": f"{progress.current_depth}/{progress.total_depth}",
                "queries": f"{progress.completed_queries}/{progress.total_queries}",
                "message": progress.message,
            }
        )

    return on_progress


def progress_summary(events: list[dict[str, Any]]) -> str:
    """The progress trail as bullet lines an agent can read at a glance."""
    lines = [
        f"- {event.get('stage', 'step')}: {event.get('message') or event.get('url', '')}".rstrip(
            ": "
        )
        for event in events
    ]
    return "\n".join(lines) or "- (no progress reported yet)"
