import httpx
import pytest
import respx

from app.core.cache import TTLCache
from app.core.search.searxng import SearxngClient
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
