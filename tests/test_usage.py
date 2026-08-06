"""Token / cost accounting (§6.5).

The point of these: the numbers must add up across a whole run — including the
reranker's own calls, which are easy to forget because they happen behind a
protocol — and accounting must never be able to break the run it measures.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.llm.usage import UsageTracker
from app.core.research.engine import DeepResearchEngine
from app.core.research.schemas import RankedResults
from app.models.usage import Usage
from tests.test_research import FakeCrawler, FakeLLM, FakeSearxng


class _Usage(SimpleNamespace):
    pass


def _response(prompt: int, completion: int) -> SimpleNamespace:
    return SimpleNamespace(usage=_Usage(prompt_tokens=prompt, completion_tokens=completion))


def test_tokens_accumulate_across_calls(monkeypatch) -> None:
    monkeypatch.setattr("app.core.llm.usage.litellm.completion_cost", lambda **_: 0.002)
    tracker = UsageTracker()
    tracker.record_completion(_response(100, 20))
    tracker.record_completion(_response(50, 10))

    assert tracker.prompt_tokens == 150
    assert tracker.completion_tokens == 30
    assert tracker.total_cost_usd == pytest.approx(0.004)


def test_a_response_without_usage_costs_nothing_and_does_not_raise(monkeypatch) -> None:
    """Many providers omit usage on streams. That must be a no-op, not a crash."""
    monkeypatch.setattr(
        "app.core.llm.usage.litellm.completion_cost",
        lambda **_: (_ for _ in ()).throw(RuntimeError("unknown model")),
    )
    tracker = UsageTracker()
    tracker.record_completion(SimpleNamespace())  # no .usage at all
    tracker.record_completion(_response(10, 1))  # usage, but unpriceable

    assert tracker.prompt_tokens == 10
    assert tracker.total_cost_usd == 0.0


def test_dict_shaped_usage_is_read() -> None:
    tracker = UsageTracker()
    tracker.record_completion(SimpleNamespace(usage={"prompt_tokens": 7, "completion_tokens": 3}))
    assert (tracker.prompt_tokens, tracker.completion_tokens) == (7, 3)


def test_snapshot_is_json_ready() -> None:
    tracker = UsageTracker(prompt_tokens=5, total_cost_usd=1 / 3)
    snapshot = tracker.snapshot()
    assert snapshot["prompt_tokens"] == 5
    assert snapshot["total_cost_usd"] == pytest.approx(0.333333)
    assert Usage.from_tracker(tracker).prompt_tokens == 5


# -- End-to-end through the engine -----------------------------------------


class CountingLLM(FakeLLM):
    """Records into whatever tracker it is handed, like the real client does."""

    def __init__(self) -> None:
        super().__init__(feedback_questions=[])
        self.usage_seen = 0

    async def structured(self, messages, response_model, *, model=None, usage=None, **kwargs):
        if usage is not None:
            self.usage_seen += 1
            usage.record_completion(_response(100, 10))
        if response_model.__name__ == "RankedResults":
            return RankedResults(keep=[0])
        return await super().structured(messages, response_model, model=model, **kwargs)

    async def complete(self, messages, *, usage=None, **kwargs) -> str:
        if usage is not None:
            self.usage_seen += 1
            usage.record_completion(_response(500, 200))
        return await super().complete(messages, **kwargs)


class OneKeptReranker:
    """Keeps one result and burns an LLM call doing it, like `LLMReranker`."""

    def __init__(self, llm) -> None:
        self._llm = llm

    async def rank(self, goal, results, top_k, *, time_range=None, usage=None):
        await self._llm.structured([], RankedResults, usage=usage)
        return results[:1]


async def test_usage_is_accumulated_across_a_whole_run(monkeypatch) -> None:
    monkeypatch.setattr("app.core.llm.usage.litellm.completion_cost", lambda **_: 0.001)
    llm = CountingLLM()
    engine = DeepResearchEngine(
        searxng=FakeSearxng(),
        crawler=FakeCrawler(),
        llm=llm,
        reranker=OneKeptReranker(llm),
        concurrency=1,
    )

    result = await engine.run("topic", breadth=2, depth=1)

    # 2 SERP queries -> 2 searches, 2 pages kept of 4 offered.
    assert result.usage.searches == 2
    assert result.usage.pages_crawled == 2
    # FakeSearxng returns 2 candidates per query and the reranker keeps 1.
    assert result.usage.pages_skipped_by_rerank == 2
    assert result.usage.prompt_tokens > 0
    assert result.usage.total_cost_usd > 0


async def test_reranker_calls_are_billed_to_the_run(monkeypatch) -> None:
    """The reranker makes its own model call; leaving it out under-reports the bill."""
    monkeypatch.setattr("app.core.llm.usage.litellm.completion_cost", lambda **_: 0.001)

    without = CountingLLM()
    engine = DeepResearchEngine(
        searxng=FakeSearxng(), crawler=FakeCrawler(), llm=without, concurrency=1
    )
    plain = await engine.run("topic", breadth=2, depth=1)

    with_rerank = CountingLLM()
    engine = DeepResearchEngine(
        searxng=FakeSearxng(),
        crawler=FakeCrawler(),
        llm=with_rerank,
        reranker=OneKeptReranker(with_rerank),
        concurrency=1,
    )
    reranked = await engine.run("topic", breadth=2, depth=1)

    assert with_rerank.usage_seen == without.usage_seen + 2  # one rerank call per SERP query
    assert reranked.usage.total_cost_usd > plain.usage.total_cost_usd


# -- LLMClient threading ----------------------------------------------------


class _Chunk(SimpleNamespace):
    pass


def _delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


async def test_client_records_a_plain_completion(monkeypatch) -> None:
    from app.core.llm.client import LLMClient

    monkeypatch.setattr("app.core.llm.usage.litellm.completion_cost", lambda **_: 0.003)
    client = LLMClient(default_model="openai/gpt-4o-mini")

    async def fake_acompletion(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=" hi "))],
            usage=_Usage(prompt_tokens=42, completion_tokens=7),
        )

    monkeypatch.setattr(client, "_acompletion", fake_acompletion)

    tracker = UsageTracker()
    assert await client.complete([{"role": "user", "content": "x"}], usage=tracker) == "hi"
    assert tracker.prompt_tokens == 42
    assert tracker.total_cost_usd == pytest.approx(0.003)


async def test_client_records_streamed_usage_from_the_final_chunk(monkeypatch) -> None:
    """Providers report a stream's usage on a last, text-free chunk."""
    from app.core.llm.client import LLMClient

    monkeypatch.setattr("app.core.llm.usage.litellm.completion_cost", lambda **_: 0.005)
    client = LLMClient(default_model="openai/gpt-4o-mini")
    seen_kwargs: dict = {}

    async def fake_acompletion(**kwargs):
        seen_kwargs.update(kwargs)

        async def stream():
            yield _delta("Hello ")
            yield _delta("world")
            # No `choices` at all on the usage chunk, as several providers send it.
            yield _Chunk(usage=_Usage(prompt_tokens=300, completion_tokens=12), choices=[])

        return stream()

    monkeypatch.setattr(client, "_acompletion", fake_acompletion)

    tracker = UsageTracker()
    tokens = [t async for t in client.stream_complete([], usage=tracker)]

    assert "".join(tokens) == "Hello world"
    # Usage on a stream is opt-in with most providers.
    assert seen_kwargs["stream_options"] == {"include_usage": True}
    assert tracker.prompt_tokens == 300
    assert tracker.total_cost_usd == pytest.approx(0.005)


async def test_a_stream_without_usage_still_yields_every_token(monkeypatch) -> None:
    """A provider that ignores `include_usage` costs us the accounting, not the call."""
    from app.core.llm.client import LLMClient

    client = LLMClient(default_model="openai/gpt-4o-mini")

    async def fake_acompletion(**kwargs):
        async def stream():
            yield _delta("a")
            yield _delta("b")

        return stream()

    monkeypatch.setattr(client, "_acompletion", fake_acompletion)

    tracker = UsageTracker()
    assert [t async for t in client.stream_complete([], usage=tracker)] == ["a", "b"]
    assert tracker.prompt_tokens == 0


async def test_no_tracker_means_no_stream_options(monkeypatch) -> None:
    """The un-instrumented path must stay byte-for-byte what it was."""
    from app.core.llm.client import LLMClient

    client = LLMClient(default_model="openai/gpt-4o-mini")
    seen_kwargs: dict = {}

    async def fake_acompletion(**kwargs):
        seen_kwargs.update(kwargs)

        async def stream():
            yield _delta("a")

        return stream()

    monkeypatch.setattr(client, "_acompletion", fake_acompletion)

    assert [t async for t in client.stream_complete([])] == ["a"]
    assert "stream_options" not in seen_kwargs
