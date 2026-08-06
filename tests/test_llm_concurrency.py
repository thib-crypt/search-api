"""The process-wide cap on in-flight LLM calls (`_concurrency_guard`).

`CRAWL_MAX_CONCURRENCY` has had a test since phase 7; `LLM_MAX_CONCURRENCY` had
none, despite bounding the other expensive resource a run consumes. The guard is
a semaphore cached per event loop, so these tests reset the cache: a semaphore
left over from another test would fix the limit before the one under test asks
for it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.llm import client as llm_client
from app.core.llm.client import LLMClient


@pytest.fixture(autouse=True)
def _fresh_guard():
    """Drop the cached semaphore so each test defines its own limit."""
    llm_client._GUARDS.clear()
    yield
    llm_client._GUARDS.clear()


class _PeakCounter:
    """Stands in for the router: records how many calls overlap."""

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    async def __call__(self, **kwargs: object) -> object:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.calls += 1
        try:
            await asyncio.sleep(self.delay)
            return _response("ok")
        finally:
            self.in_flight -= 1


def _response(text: str) -> object:
    class _Msg:
        content = text

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]
        usage = None

    return _Resp()


def _client(max_concurrency: int, backend: _PeakCounter) -> LLMClient:
    client = LLMClient("gpt-4o-mini", max_concurrency=max_concurrency)
    client._acompletion = backend  # type: ignore[method-assign]
    return client


async def test_concurrent_completions_never_exceed_the_cap() -> None:
    backend = _PeakCounter()
    client = _client(3, backend)

    results = await asyncio.gather(
        *(client.complete([{"role": "user", "content": "hi"}]) for _ in range(20))
    )

    assert results == ["ok"] * 20
    assert backend.calls == 20
    assert backend.peak <= 3


async def test_a_cap_of_zero_is_clamped_rather_than_deadlocking() -> None:
    """A semaphore of 0 blocks forever; the client clamps it to 1 like the crawler."""
    backend = _PeakCounter()
    client = _client(0, backend)

    assert client.max_concurrency == 1
    await asyncio.wait_for(
        asyncio.gather(*(client.complete([{"role": "user", "content": "hi"}]) for _ in range(3))),
        timeout=2.0,
    )
    assert backend.peak == 1


async def test_the_cap_is_process_wide_not_per_client() -> None:
    """Two clients on one loop share one budget -- the point of a *process* cap.

    Also pins the consequence: the limit is fixed by whichever client makes the
    first call on the loop, and a later client asking for a different one does
    not raise it. The deployment builds exactly one `LLMClient`, so this is a
    documented property rather than a problem; a test states it so a second
    client cannot be added later on the assumption that its own limit applies.
    """
    backend = _PeakCounter()
    first = _client(2, backend)
    second = _client(8, backend)

    await asyncio.gather(
        *(first.complete([{"role": "user", "content": "a"}]) for _ in range(6)),
        *(second.complete([{"role": "user", "content": "b"}]) for _ in range(6)),
    )

    assert backend.calls == 12
    assert backend.peak <= 2  # not 8, and not 2 + 8
