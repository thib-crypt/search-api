"""Web search endpoint backed by SearXNG.

`POST /v1/search` is the canonical form. `GET /v1/search` (§5.10) exposes the same
parameters as a query string so the endpoint can be tried from a browser or curl,
and — the real point — so a reverse proxy or CDN in front of the API can cache it
with `Cache-Control` + `ETag` and answer repeats without touching this process.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.deps import get_searxng, require_api_key
from app.config import Settings, get_settings
from app.core.search.searxng import SearxngClient, SearxngError
from app.models.search import SafeSearch, SearchRequest, SearchResponse, TimeRange

router = APIRouter(prefix="/v1", tags=["search"], dependencies=[Depends(require_api_key)])

# Short enough that a cached SERP is never badly stale, long enough that a burst
# of identical requests collapses at the proxy.
_MAX_AGE = 60


def _csv(values: list[str] | None) -> list[str] | None:
    """Accept both repeated params (`?categories=a&categories=b`) and CSV (`?categories=a,b`)."""
    if not values:
        return None
    out = [part.strip() for value in values for part in value.split(",") if part.strip()]
    return out or None


@router.post("/search", response_model=SearchResponse, summary="Web search via SearXNG")
async def search(
    req: SearchRequest,
    client: SearxngClient = Depends(get_searxng),
) -> SearchResponse:
    try:
        return await client.search(req)
    except SearxngError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Web search via SearXNG (cacheable GET variant)",
    responses={304: {"description": "The ETag matched If-None-Match; body omitted."}},
)
async def search_get(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query."),
    categories: list[str] | None = Query(
        default=None, description="Repeated or comma-separated, e.g. ?categories=general,news"
    ),
    engines: list[str] | None = Query(default=None, description="Repeated or comma-separated."),
    language: str | None = Query(default=None),
    pageno: int = Query(default=1, ge=1),
    time_range: TimeRange | None = Query(default=None),
    safesearch: SafeSearch | None = Query(default=None),
    max_results: int | None = Query(default=None, ge=1, le=100),
    include_domains: list[str] | None = Query(
        default=None, description="Repeated or comma-separated."
    ),
    exclude_domains: list[str] | None = Query(
        default=None, description="Repeated or comma-separated."
    ),
    client: SearxngClient = Depends(get_searxng),
    settings: Settings = Depends(get_settings),
) -> Response:
    req = SearchRequest(
        q=q,
        categories=_csv(categories),
        engines=_csv(engines),
        language=language,
        pageno=pageno,
        time_range=time_range,
        safesearch=safesearch,
        max_results=max_results,
        include_domains=_csv(include_domains),
        exclude_domains=_csv(exclude_domains),
    )
    try:
        result = await client.search(req)
    except SearxngError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    body = result.model_dump_json().encode()
    etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    # `private` once auth is on: an authenticated response must not be handed to
    # another key by a shared cache. Without auth every caller is equivalent, so
    # a shared proxy cache is both safe and the whole point of this variant.
    scope = "private" if settings.auth_enabled else "public"
    headers = {"Cache-Control": f"{scope}, max-age={_MAX_AGE}", "ETag": etag}

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
