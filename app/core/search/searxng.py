"""Async client for a self-hosted SearXNG instance.

Talks to SearXNG's JSON search API (`GET /search?format=json`). The bundled
instance has `formats: [html, json]` enabled and the limiter disabled, since the
API is its only client.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.core.cache import TTLCache
from app.models.search import SearchRequest, SearchResponse, SearchResultItem


class SearxngError(RuntimeError):
    """Raised when the SearXNG backend cannot be reached or returns an error."""


class SearxngClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        *,
        cache_ttl: float = 0.0,
        cache_max_size: int = 512,
        max_connections: int = 64,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Bounded keep-alive pool: identical concurrent searches (common during
        # deep research) reuse connections instead of opening a socket each time.
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"User-Agent": "unified-search-api/0.1"},
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
            ),
        )
        # Memoize identical queries for a short window. The research engine fans
        # out overlapping SERP queries, so this cuts redundant SearXNG round-trips.
        self._cache: TTLCache[SearchResponse] | None = (
            TTLCache(ttl=cache_ttl, max_size=cache_max_size) if cache_ttl > 0 else None
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build_params(self, req: SearchRequest) -> dict[str, str]:
        params: dict[str, str] = {"q": req.q, "format": "json", "pageno": str(req.pageno)}
        if req.categories:
            params["categories"] = ",".join(req.categories)
        if req.engines:
            params["engines"] = ",".join(req.engines)
        if req.language:
            params["language"] = req.language
        if req.time_range:
            params["time_range"] = req.time_range
        if req.safesearch is not None:
            params["safesearch"] = str(req.safesearch)
        return params

    async def search(self, req: SearchRequest) -> SearchResponse:
        params = self._build_params(req)
        cache_key = self._cache_key(req, params) if self._cache else None
        if self._cache is not None and cache_key is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            resp = await self._client.get("/search", params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SearxngError(
                f"SearXNG returned {exc.response.status_code}. "
                "Is the JSON format enabled in settings.yml?"
            ) from exc
        except httpx.HTTPError as exc:
            raise SearxngError(f"Could not reach SearXNG at {self._base_url}: {exc}") from exc

        response = self._parse(req, resp.json())
        if self._cache is not None and cache_key is not None:
            await self._cache.set(cache_key, response)
        return response

    @staticmethod
    def _cache_key(req: SearchRequest, params: dict[str, str]) -> str:
        # `format` is constant; `max_results` is applied client-side (not in params)
        # but changes the response, so fold it into the key.
        items = sorted((k, v) for k, v in params.items() if k != "format")
        items.append(("max_results", str(req.max_results)))
        return urlencode(items)

    @staticmethod
    def _parse(req: SearchRequest, data: dict) -> SearchResponse:
        raw_results = data.get("results", []) or []
        items: list[SearchResultItem] = []
        for r in raw_results:
            url = r.get("url")
            if not url:
                continue
            items.append(
                SearchResultItem(
                    title=r.get("title") or url,
                    url=url,
                    snippet=r.get("content") or "",
                    engine=r.get("engine"),
                    score=r.get("score"),
                    category=r.get("category"),
                    published_date=r.get("publishedDate"),
                )
            )

        if req.max_results is not None:
            items = items[: req.max_results]

        suggestions = [s for s in (data.get("suggestions") or []) if isinstance(s, str)]
        answers = [
            a.get("answer") if isinstance(a, dict) else a
            for a in (data.get("answers") or [])
        ]
        answers = [a for a in answers if isinstance(a, str)]

        return SearchResponse(
            query=req.q,
            number_of_results=len(items),
            results=items,
            suggestions=suggestions,
            answers=answers,
        )
