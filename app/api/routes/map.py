"""Fast URL-discovery endpoint (§5.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_crawler, get_url_guard, require_api_key
from app.config import Settings, get_settings
from app.core.crawl.crawler import CrawlerService
from app.core.net.guard import BlockedUrlError, UrlGuard
from app.models.map import MapRequest, MapResponse
from app.services.mapper import MapService

router = APIRouter(prefix="/v1", tags=["map"], dependencies=[Depends(require_api_key)])


@router.post(
    "/map",
    response_model=MapResponse,
    summary="Discover a site's URLs (sitemap.xml, robots.txt, then page links)",
)
async def map_site(
    req: MapRequest,
    crawler: CrawlerService = Depends(get_crawler),
    guard: UrlGuard = Depends(get_url_guard),
    settings: Settings = Depends(get_settings),
) -> MapResponse:
    service = MapService(crawler=crawler, guard=guard, timeout=settings.map_timeout)
    try:
        return await service.map_site(req)
    except BlockedUrlError as exc:
        raise HTTPException(status_code=400, detail=f"URL not allowed: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Map failed: {exc}") from exc
