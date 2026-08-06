import asyncio

import httpx
import pytest
import respx

from app.core.cache import TTLCache
from app.core.search.searxng import SearxngClient, SearxngError
from app.models.search import SearchRequest

SEARXNG_JSON = {
    "query": "python",
    "results": [{"url": "https://python.org", "title": "Python", "content": "Official."}],
    "suggestions": [],
    "answers": [],
}


@pytest.mark.asyncio
async def test_ttlcache_evicts_lru_over_max_size() -> None:
    cache: TTLCache[int] = TTLCache(ttl=60.0, max_size=2)
    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.get("a")  # touch "a" so "b" becomes least-recently used
    await cache.set("c", 3)  # exceeds max_size -> evicts "b"

    assert await cache.get("a") == 1
    assert await cache.get("b") is None
    assert await cache.get("c") == 3


@pytest.mark.asyncio
async def test_ttlcache_expires_entries() -> None:
    cache: TTLCache[int] = TTLCache(ttl=-1.0, max_size=8)  # already-expired entries
    await cache.set("a", 1)
    assert await cache.get("a") is None


@respx.mock
@pytest.mark.asyncio
async def test_searxng_caches_identical_queries() -> None:
    route = respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    client = SearxngClient("http://searxng:8080", cache_ttl=60.0)
    try:
        req = SearchRequest(q="python", max_results=5)
        first = await client.search(req)
        second = await client.search(SearchRequest(q="python", max_results=5))
    finally:
        await client.aclose()

    assert route.call_count == 1  # second call served from cache
    assert first.results[0].url == second.results[0].url


class _SlowBackend:
    """Counts SearXNG hits and keeps each one in flight long enough to overlap."""

    def __init__(self, *, delay: float = 0.05, fail: bool = False) -> None:
        self.calls = 0
        self._delay = delay
        self._fail = fail

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        await asyncio.sleep(self._delay)
        if self._fail:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=SEARXNG_JSON)


@respx.mock
@pytest.mark.asyncio
async def test_searxng_coalesces_concurrent_identical_queries() -> None:
    backend = _SlowBackend()
    respx.get("http://searxng:8080/search").mock(side_effect=backend)
    client = SearxngClient("http://searxng:8080", cache_ttl=60.0)
    try:
        responses = await asyncio.gather(
            *(client.search(SearchRequest(q="python", max_results=5)) for _ in range(8))
        )
    finally:
        await client.aclose()

    assert backend.calls == 1  # single-flight: 7 callers joined the in-flight request
    assert all(r.results[0].url == "https://python.org" for r in responses)
    # Each caller owns its copy, so mutating one cannot leak into the others.
    assert len({id(r) for r in responses}) == len(responses)
    assert len({id(r.results[0]) for r in responses}) == len(responses)


@respx.mock
@pytest.mark.asyncio
async def test_searxng_coalesces_concurrent_queries_without_cache() -> None:
    backend = _SlowBackend()
    respx.get("http://searxng:8080/search").mock(side_effect=backend)
    client = SearxngClient("http://searxng:8080", cache_ttl=0.0)  # cache disabled
    try:
        await asyncio.gather(*(client.search(SearchRequest(q="python")) for _ in range(6)))
        # Registry is emptied once the flight settles, so a later call re-fetches.
        await client.search(SearchRequest(q="python"))
    finally:
        await client.aclose()

    assert backend.calls == 2


@respx.mock
@pytest.mark.asyncio
async def test_searxng_single_flight_propagates_errors_to_all_waiters() -> None:
    backend = _SlowBackend(fail=True)
    respx.get("http://searxng:8080/search").mock(side_effect=backend)
    client = SearxngClient("http://searxng:8080", cache_ttl=60.0)
    try:
        results = await asyncio.gather(
            *(client.search(SearchRequest(q="python")) for _ in range(4)),
            return_exceptions=True,
        )
        # A failed flight leaves no stale registry entry: the next call re-fetches.
        with pytest.raises(SearxngError):
            await client.search(SearchRequest(q="python"))
    finally:
        await client.aclose()

    assert backend.calls == 2  # one shared failure, then one retry
    assert len(results) == 4
    assert all(isinstance(r, SearxngError) for r in results)


@respx.mock
@pytest.mark.asyncio
async def test_searxng_single_flight_never_strands_waiters_on_a_dead_leader() -> None:
    """A leader that dies *after* fetching must not leave its waiters hanging.

    The cache write sits between the fetch and `set_result`, so a failure (or a
    cancellation) there could otherwise unregister a future nobody ever settles.
    """
    backend = _SlowBackend()
    respx.get("http://searxng:8080/search").mock(side_effect=backend)
    client = SearxngClient("http://searxng:8080", cache_ttl=60.0)
    original_set = client._cache.set
    writes = 0

    async def flaky_set(key: str, value: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise RuntimeError("cache write failed")
        await original_set(key, value)

    client._cache.set = flaky_set  # type: ignore[method-assign]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(client.search(SearchRequest(q="python")) for _ in range(4)),
                return_exceptions=True,
            ),
            timeout=5.0,  # a stranded waiter would block here forever
        )
    finally:
        await client.aclose()

    # The leader surfaces its error; the waiters retry rather than hang.
    assert sum(isinstance(r, RuntimeError) for r in results) == 1
    assert [r.results[0].url for r in results if not isinstance(r, BaseException)] == [
        "https://python.org"
    ] * 3


@respx.mock
@pytest.mark.asyncio
async def test_searxng_cache_hit_returns_isolated_copy() -> None:
    respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    client = SearxngClient("http://searxng:8080", cache_ttl=60.0)
    try:
        first = await client.search(SearchRequest(q="python"))
        first.results.clear()  # a caller mutating its response...
        second = await client.search(SearchRequest(q="python"))
    finally:
        await client.aclose()

    assert second.results[0].url == "https://python.org"  # ...must not poison the cache


@respx.mock
@pytest.mark.asyncio
async def test_searxng_cache_disabled_always_hits_backend() -> None:
    route = respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json=SEARXNG_JSON)
    )
    client = SearxngClient("http://searxng:8080", cache_ttl=0.0)
    try:
        await client.search(SearchRequest(q="python"))
        await client.search(SearchRequest(q="python"))
    finally:
        await client.aclose()

    assert route.call_count == 2
