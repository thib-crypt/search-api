"""Async client for a self-hosted SearXNG instance.

Talks to SearXNG's JSON search API (`GET /search?format=json`). The bundled
instance has `formats: [html, json]` enabled and the limiter disabled, since the
API is its only client.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from app.core import metrics
from app.core.cache import TTLCache
from app.core.net.guard import host_matches
from app.models.search import SearchRequest, SearchResponse, SearchResultItem


class SearxngError(RuntimeError):
    """Raised when the SearXNG backend cannot be reached or returns an error."""


def _domain_allowed(url: str, include: set[str], exclude: set[str]) -> bool:
    """Apply the include/exclude domain filters (§5.6) to one result URL.

    Host matching is shared with the SSRF guard, so "example.com" covers
    "www.example.com" here exactly as it does in the allow/deny lists.
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if exclude and host_matches(host, exclude):
        return False
    if include and not host_matches(host, include):
        return False
    return True


def _mark_exception_retrieved(fut: asyncio.Future[SearchResponse]) -> None:
    """Silence "exception was never retrieved" when no waiter joined the flight.

    Real waiters still see the exception: reading it here only clears asyncio's
    unretrieved-traceback flag.
    """
    if not fut.cancelled():
        fut.exception()


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
        # Single-flight registry: cache key -> future of the fetch currently in
        # flight for that key. Concurrent identical searches join the in-flight
        # request instead of issuing their own round-trip. Useful even with the
        # cache disabled, since the duplicate work is wasteful either way.
        self._inflight: dict[str, asyncio.Future[SearchResponse]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ping(self, probe_timeout: float = 2.0) -> None:
        """Cheap reachability probe for `/health/ready` (§5.9).

        Deliberately not a search: readiness must not cost a real SERP round-trip
        on every probe. Any answer from the process counts as reachable — only a
        transport failure or a 5xx means "not ready".
        """
        try:
            resp = await self._client.get("/healthz", timeout=probe_timeout)
        except httpx.HTTPError as exc:
            raise SearxngError(f"Could not reach SearXNG at {self._base_url}: {exc}") from exc
        if resp.status_code >= 500:
            raise SearxngError(f"SearXNG returned {resp.status_code}")

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
        cache_key = self._cache_key(req, params)

        while True:
            if self._cache is not None:
                cached = await self._cache.get(cache_key)
                if cached is not None:
                    metrics.record_cache("search", hit=True)
                    # Hand out a private copy: the cached model stays pristine even
                    # if the caller mutates what it got back.
                    return cached.model_copy(deep=True)

            inflight = self._inflight.get(cache_key)
            if inflight is None:
                metrics.record_cache("search", hit=False)
                break

            # Wait without adopting the future's cancellation: `asyncio.wait`
            # returns when it settles and only raises if *we* get cancelled.
            await asyncio.wait([inflight])
            if not inflight.cancelled():
                # Counted as a hit: like a cache hit, a coalesced waiter costs no
                # round-trip to SearXNG. Recording it keeps hits + misses equal to
                # the number of searches asked for.
                metrics.record_cache("search", hit=True)
                # Raises here if the flight failed, so every waiter sees the error.
                return inflight.result().model_copy(deep=True)
            # The leader was cancelled before finishing; retry (its registry entry
            # is already gone, so we either join a new leader or become one).

        # No awaits between the miss above and registering the future, so no other
        # task can slip in and start a duplicate fetch for this key.
        future: asyncio.Future[SearchResponse] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_mark_exception_retrieved)
        self._inflight[cache_key] = future
        try:
            response = await self._fetch(req, params)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            if self._cache is not None:
                await self._cache.set(cache_key, response)
            future.set_result(response)
            # `response` is the shared/canonical object (cache + waiters); the
            # caller gets its own copy, like every other path.
            return response.model_copy(deep=True)
        finally:
            # Runs after the cache is populated, so a task arriving right behind us
            # finds either the cached value or a live flight — never a gap.
            if self._inflight.get(cache_key) is future:
                del self._inflight[cache_key]
            # Belt and braces: if we leave without settling the future (e.g. the
            # cache write above raised, or cancellation landed between the fetch
            # and `set_result`), cancel it so waiters retry instead of blocking
            # forever on a future nobody will ever complete.
            if not future.done():
                future.cancel()

    async def _fetch(self, req: SearchRequest, params: dict[str, str]) -> SearchResponse:
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

        return self._parse(req, resp.json())

    @staticmethod
    def _cache_key(req: SearchRequest, params: dict[str, str]) -> str:
        # `format` is constant; `max_results` is applied client-side (not in params)
        # but changes the response, so fold it into the key.
        items = sorted((k, v) for k, v in params.items() if k != "format")
        items.append(("max_results", str(req.max_results)))
        # The domain filters are applied client-side, so they never reach `params`
        # yet change the response: they belong in the key.
        items.append(("include_domains", ",".join(sorted(req.include_domains or []))))
        items.append(("exclude_domains", ",".join(sorted(req.exclude_domains or []))))
        return urlencode(items)

    @staticmethod
    def _parse(req: SearchRequest, data: dict[str, Any]) -> SearchResponse:
        raw_results = data.get("results", []) or []
        include = {d.strip().lower().lstrip(".") for d in (req.include_domains or []) if d.strip()}
        exclude = {d.strip().lower().lstrip(".") for d in (req.exclude_domains or []) if d.strip()}
        items: list[SearchResultItem] = []
        for r in raw_results:
            url = r.get("url")
            if not url:
                continue
            # Filtered before `max_results` is applied, so a caller asking for 5
            # results on one domain gets 5 of them, not 5 minus whatever was dropped.
            if (include or exclude) and not _domain_allowed(url, include, exclude):
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
        raw_answers = [
            a.get("answer") if isinstance(a, dict) else a for a in (data.get("answers") or [])
        ]
        answers = [a for a in raw_answers if isinstance(a, str)]

        return SearchResponse(
            query=req.q,
            number_of_results=len(items),
            results=items,
            suggestions=suggestions,
            answers=answers,
        )
