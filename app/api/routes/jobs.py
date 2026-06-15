"""Job inspection and SSE progress streaming."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_jobs, require_api_key
from app.jobs.manager import JobManager
from app.models.jobs import JobView

router = APIRouter(prefix="/v1/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])


@router.get("/{job_id}", response_model=JobView, summary="Get a job's status and result")
async def get_job(job_id: str, jobs: JobManager = Depends(get_jobs)) -> JobView:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JobView(**job.public())


@router.get("/{job_id}/stream", summary="Stream job progress as Server-Sent Events")
async def stream_job(job_id: str, jobs: JobManager = Depends(get_jobs)) -> EventSourceResponse:
    if jobs.get(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def event_source():
        async for event in jobs.stream(job_id):
            yield {"event": event.get("event", "progress"), "data": json.dumps(event)}

    return EventSourceResponse(event_source())
