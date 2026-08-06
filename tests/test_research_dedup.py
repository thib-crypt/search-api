"""URL normalization, cross-branch URL dedup, and near-duplicate page dropping."""

import pytest

from app.core.research.dedup import dedupe_contents, normalize_url
from app.core.research.engine import DeepResearchEngine
from app.models.crawl import CrawledPage
from app.models.search import SearchResponse, SearchResultItem
from tests.test_research import FakeLLM


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.COM/Path/", "https://example.com/Path"),
        ("https://example.com/path#section", "https://example.com/path"),
        ("https://example.com/path?utm_source=x&utm_medium=y", "https://example.com/path"),
        ("https://example.com/path?fbclid=1&gclid=2&ref=nav", "https://example.com/path"),
        ("https://example.com/path?id=7&utm_campaign=z", "https://example.com/path?id=7"),
        ("https://example.com/", "https://example.com"),
        ("https://example.com", "https://example.com"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_url_keeps_meaningful_query_and_survives_garbage() -> None:
    assert normalize_url("https://a.com/s?q=deep+research") == "https://a.com/s?q=deep+research"
    assert normalize_url("") == ""
    assert normalize_url("not a url/") == "not a url"


def test_two_urls_differing_only_by_tracking_are_the_same_page() -> None:
    a = normalize_url("https://site.com/story?utm_source=twitter#top")
    b = normalize_url("https://SITE.com/story/")
    assert a == b


# -- Content dedup ---------------------------------------------------------

_STORY = (
    "Reuters reported on Tuesday that the central bank raised its policy rate by "
    "twenty five basis points, the third increase this year, citing persistent "
    "services inflation across the euro area."
)


def test_dedupe_contents_drops_republished_story() -> None:
    republished = _STORY + " Additional local commentary follows."
    other = (
        "A completely different article about deep sea mining permits in the "
        "Pacific, licences, and environmental impact assessments filed in June."
    )
    kept, dropped = dedupe_contents([_STORY, republished, other])

    assert kept == [0, 2]
    assert dropped == [1]


def test_dedupe_contents_keeps_distinct_pages() -> None:
    kept, dropped = dedupe_contents(["alpha beta gamma delta epsilon zeta", _STORY])
    assert kept == [0, 1]
    assert dropped == []


def test_dedupe_contents_handles_empty_and_short_inputs() -> None:
    kept, dropped = dedupe_contents(["", "", "short text"])
    # Empty pages have no shingles at all, so they cannot claim to duplicate anything.
    assert kept == [0, 1, 2]
    assert dropped == []


# -- Cross-branch URL dedup ------------------------------------------------


class SharedSearxng:
    """Every query returns the same two links, as neighbouring SERPs really do."""

    async def search(self, req) -> SearchResponse:
        items = [
            SearchResultItem(title="Shared A", url="https://shared.com/a?utm_source=serp"),
            SearchResultItem(title="Shared B", url="https://shared.com/b"),
        ]
        return SearchResponse(query=req.q, number_of_results=2, results=items)


class RecordingCrawler:
    def __init__(self) -> None:
        self.crawled: list[str] = []

    async def crawl(self, req) -> list[CrawledPage]:
        urls = [str(u) for u in req.urls]
        self.crawled.extend(urls)
        return [
            CrawledPage(url=u, success=True, status_code=200, markdown=f"body of {u}") for u in urls
        ]


@pytest.mark.asyncio
async def test_url_crawled_by_one_branch_is_not_recrawled_by_another() -> None:
    crawler = RecordingCrawler()
    engine = DeepResearchEngine(
        searxng=SharedSearxng(), crawler=crawler, llm=FakeLLM(), concurrency=1
    )

    result = await engine.run("topic", breadth=2, depth=1)

    # Two branches, one shared SERP: each page is fetched exactly once.
    assert sorted(crawler.crawled) == [
        "https://shared.com/a?utm_source=serp",
        "https://shared.com/b",
    ]
    assert len(result.sources) == 2
    assert result.serp_queries == ["q1a", "q1b"]  # the second branch still ran
