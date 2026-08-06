"""CrawlerService invariants: bounded sessions, one browser pool, exact pairing.

The browser is replaced by a stub — these are tests about the service's own
bookkeeping, not about crawl4ai.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.crawl.crawler import CrawlerService
from app.core.net.guard import BlockedUrlError, UrlGuard
from app.models.crawl import CrawlRequest


def _ok(url: str, markdown: str = "body"):
    return SimpleNamespace(
        success=True,
        url=url,
        status_code=200,
        markdown=SimpleNamespace(raw_markdown=markdown, fit_markdown=markdown),
        metadata={"title": "T"},
        links={"internal": [], "external": []},
    )


class StubBrowser:
    """Records concurrency and lets a test decide what each URL returns."""

    def __init__(self, *, delay: float = 0.01, behavior=None) -> None:
        self.delay = delay
        self.behavior = behavior or (lambda url: _ok(url))
        self.in_flight = 0
        self.peak = 0
        self.starts = 0
        self.seen: list[str] = []

    async def start(self) -> None:
        self.starts += 1
        await asyncio.sleep(0.01)  # a real browser pool takes a moment to come up

    async def close(self) -> None:
        pass

    async def arun(self, url: str, config=None):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.seen.append(url)
        try:
            await asyncio.sleep(self.delay)
            return self.behavior(url)
        finally:
            self.in_flight -= 1


def _service(browser: StubBrowser, *, max_concurrency: int = 3) -> CrawlerService:
    service = CrawlerService(
        max_concurrency=max_concurrency,
        # allow_private: the stub never touches the network, so skip DNS.
        guard=UrlGuard(allow_private=True),
        per_host_delay=0.0,
    )
    service._crawler = browser
    return service


async def test_concurrent_sessions_never_exceed_the_cap() -> None:
    browser = StubBrowser()
    service = _service(browser, max_concurrency=3)
    urls = [f"https://host{i}.example/page" for i in range(20)]

    pages = await service.crawl(CrawlRequest(urls=urls))

    assert len(pages) == 20
    assert browser.peak <= 3


async def test_cap_is_global_not_per_request() -> None:
    """Concurrent requests share one budget — this is what §2.3 was about."""
    browser = StubBrowser()
    service = _service(browser, max_concurrency=2)

    await asyncio.gather(
        *(
            service.crawl(
                CrawlRequest(urls=[f"https://r{r}-{i}.example/" for i in range(5)])
            )
            for r in range(4)
        )
    )

    assert browser.peak <= 2


async def test_start_is_called_once_under_concurrent_first_requests() -> None:
    browser = StubBrowser()
    service = _service(browser)

    await asyncio.gather(*(service.start() for _ in range(10)))

    assert browser.starts == 1


async def test_results_are_paired_with_the_requested_url() -> None:
    """A result carrying a different URL must not shift the whole batch."""

    def behavior(url: str):
        if "b.example" in url:
            # Redirected: crawl4ai reports the final URL, not the requested one.
            return _ok("https://elsewhere.example/moved", markdown="B body")
        return _ok(url, markdown=f"body for {url}")

    browser = StubBrowser(behavior=behavior)
    service = _service(browser)
    urls = ["https://a.example/", "https://b.example/", "https://c.example/"]

    pages = await service.crawl(CrawlRequest(urls=urls))

    assert [p.markdown for p in pages] == [
        "body for https://a.example/",
        "B body",
        "body for https://c.example/",
    ]


async def test_one_failing_url_does_not_sink_the_batch() -> None:
    def behavior(url: str):
        if "bad" in url:
            raise RuntimeError("browser exploded")
        return _ok(url)

    browser = StubBrowser(behavior=behavior)
    service = _service(browser)
    pages = await service.crawl(
        CrawlRequest(urls=["https://good.example/", "https://bad.example/"])
    )

    assert pages[0].success is True
    assert pages[1].success is False
    assert "browser exploded" in (pages[1].error or "")


async def test_blocked_urls_are_reported_per_url_when_others_are_allowed() -> None:
    browser = StubBrowser()
    service = CrawlerService(guard=UrlGuard(), per_host_delay=0.0)
    service._crawler = browser

    pages = await service.crawl(
        CrawlRequest(urls=["https://8.8.8.8/ok", "http://169.254.169.254/meta"])
    )

    assert [p.url for p in pages] == ["https://8.8.8.8/ok", "http://169.254.169.254/meta"]
    assert pages[0].success is True
    assert pages[1].success is False
    assert pages[1].error is not None and pages[1].error.startswith("blocked:")
    assert browser.seen == ["https://8.8.8.8/ok"]  # the blocked URL never reached the browser


async def test_all_blocked_raises_so_the_route_can_answer_400() -> None:
    service = CrawlerService(guard=UrlGuard(), per_host_delay=0.0)
    service._crawler = StubBrowser()

    with pytest.raises(BlockedUrlError):
        await service.crawl(CrawlRequest(urls=["http://169.254.169.254/meta"]))


async def test_redirect_into_a_private_address_is_dropped() -> None:
    """The guard runs again on where the crawl landed, not just where it aimed."""

    browser = StubBrowser(behavior=lambda url: _ok("http://127.0.0.1:8080/admin", "secrets"))
    service = CrawlerService(guard=UrlGuard(), per_host_delay=0.0)
    service._crawler = browser

    pages = await service.crawl(CrawlRequest(urls=["https://8.8.8.8/redirector"]))

    assert pages[0].success is False
    assert "redirected to a blocked URL" in (pages[0].error or "")
    assert "secrets" not in (pages[0].markdown or "")


async def test_deep_research_run_respects_the_browser_session_cap() -> None:
    """Phase 7 acceptance criterion, end to end.

    A `depth=3` run drives the real CrawlerService (only the browser is stubbed):
    engine recursion, per-branch fan-out and the crawler's own batching all share
    one budget.
    """
    from app.core.research.engine import DeepResearchEngine
    from tests.test_research import FakeLLM, FakeSearxng

    # The run naturally reaches 4 concurrent fetches (2 branches × 2 results),
    # so a cap of 2 genuinely binds.
    max_concurrency = 2
    browser = StubBrowser()
    crawler = _service(browser, max_concurrency=max_concurrency)
    engine = DeepResearchEngine(
        searxng=FakeSearxng(), crawler=crawler, llm=FakeLLM(), concurrency=2
    )

    await engine.run("topic", breadth=4, depth=3)

    assert browser.peak <= max_concurrency
    assert browser.seen  # the run really did crawl


async def test_same_host_requests_are_spaced_out() -> None:
    browser = StubBrowser(delay=0.0)
    service = CrawlerService(
        max_concurrency=5, guard=UrlGuard(allow_private=True), per_host_delay=0.05
    )
    service._crawler = browser

    loop = asyncio.get_running_loop()
    started = loop.time()
    await service.crawl(
        CrawlRequest(urls=[f"https://same.example/{i}" for i in range(3)])
    )
    same_host = loop.time() - started

    started = loop.time()
    await service.crawl(
        CrawlRequest(urls=[f"https://host{i}.example/" for i in range(3)])
    )
    distinct_hosts = loop.time() - started

    assert same_host >= 0.1  # two waits of 50 ms
    assert distinct_hosts < 0.05  # different hosts are never delayed
