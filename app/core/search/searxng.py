"""Async client for a self-hosted SearXNG instance.

Talks to SearXNG's JSON search API (`GET /search?format=json`). The bundled
instance has `formats: [html, json]` enabled and the limiter disabled, since the
API is its only client.
"""

from __future__ import annotations

import httpx

from app.models.search import SearchRequest, SearchResponse, SearchResultItem


class SearxngError(RuntimeError):
    """Raised when the SearXNG backend cannot be reached or returns an error."""


class SearxngClient:
    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"User-Agent": "unified-search-api/0.1"},
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
        try:
            resp = await self._client.get("/search", params=self._build_params(req))
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SearxngError(
                f"SearXNG returned {exc.response.status_code}. "
                "Is the JSON format enabled in settings.yml?"
            ) from exc
        except httpx.HTTPError as exc:
            raise SearxngError(f"Could not reach SearXNG at {self._base_url}: {exc}") from exc

        return self._parse(req, resp.json())

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
