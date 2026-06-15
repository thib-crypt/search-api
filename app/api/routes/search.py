"""Web search endpoint backed by SearXNG."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_searxng, require_api_key
from app.core.search.searxng import SearxngClient, SearxngError
from app.models.search import SearchRequest, SearchResponse

router = APIRouter(prefix="/v1", tags=["search"], dependencies=[Depends(require_api_key)])


@router.post("/search", response_model=SearchResponse, summary="Web search via SearXNG")
async def search(
    req: SearchRequest,
    client: SearxngClient = Depends(get_searxng),
) -> SearchResponse:
    try:
        return await client.search(req)
    except SearxngError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
