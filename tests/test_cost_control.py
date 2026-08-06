"""Cost circuit breakers (§6.4).

Three independent controls, tested for the behaviour that actually protects the
bill: a run that stops early still returns a usable report, a key over its daily
budget is turned away *before* it spends anything (while every other key keeps
working), and `estimate_only` prices a run without touching the network.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.api.deps import get_cost_ledger, get_crawler, get_llm, get_reranker, get_searxng
from app.config import get_settings
from app.core.cost import InMemoryCostLedger, SqliteCostLedger, build_cost_ledger, utc_day
from app.core.research.engine import DeepResearchEngine
from app.core.research.estimate import _total_queries, estimate_run
from app.core.research.rerank import NoopReranker
from app.main import app
from tests.test_research import FakeCrawler, FakeLLM, FakeSearxng
from tests.test_usage import CountingLLM

# -- a) Per-run cap ---------------------------------------------------------


class ExpensiveLLM(CountingLLM):
    """Every model call costs a flat $1, so the cap is crossed immediately."""

    async def structured(self, messages, response_model, *, model=None, usage=None, **kwargs):
        self.usage_seen += 1
        result = await FakeLLM.structured(self, messages, response_model, model=model, **kwargs)
        if usage is not None:
            usage.total_cost_usd += 1.0
        return result

    async def complete(self, messages, *, usage=None, **kwargs) -> str:
        self.usage_seen += 1
        if usage is not None:
            usage.total_cost_usd += 1.0
        return await FakeLLM.complete(self, messages, **kwargs)


async def test_a_run_over_its_cap_stops_early_and_still_writes_a_report() -> None:
    engine = DeepResearchEngine(
        searxng=FakeSearxng(),
        crawler=FakeCrawler(),
        llm=ExpensiveLLM(),
        concurrency=1,
        max_cost_usd=1.5,  # crossed after the first SERP-generation call
    )

    result = await engine.run("topic", breadth=4, depth=3)

    assert result.stopped_early is True
    # A truncated run is a shorter answer, not an error: the report is written
    # from whatever was gathered, and it says so.
    assert result.report
    assert "cost ceiling" in result.report
    assert result.usage.total_cost_usd >= 1.5


async def test_no_cap_means_no_early_stop() -> None:
    engine = DeepResearchEngine(
        searxng=FakeSearxng(), crawler=FakeCrawler(), llm=ExpensiveLLM(), concurrency=1
    )
    result = await engine.run("topic", breadth=2, depth=1)

    assert result.stopped_early is False
    assert "cost ceiling" not in result.report


async def test_the_cap_bounds_the_work_actually_done() -> None:
    """Not just a flag: a capped run must make fewer calls than an uncapped one."""
    uncapped_llm = ExpensiveLLM()
    await DeepResearchEngine(
        searxng=FakeSearxng(), crawler=FakeCrawler(), llm=uncapped_llm, concurrency=1
    ).run("topic", breadth=4, depth=2)

    capped_llm = ExpensiveLLM()
    await DeepResearchEngine(
        searxng=FakeSearxng(),
        crawler=FakeCrawler(),
        llm=capped_llm,
        concurrency=1,
        max_cost_usd=2.0,
    ).run("topic", breadth=4, depth=2)

    assert capped_llm.usage_seen < uncapped_llm.usage_seen


# -- b) Daily cap per key ---------------------------------------------------


async def test_in_memory_ledger_accumulates_per_label() -> None:
    ledger = InMemoryCostLedger()
    await ledger.add("app-a", 0.5)
    await ledger.add("app-a", 0.25)
    await ledger.add("app-b", 1.0)

    assert await ledger.spent_today("app-a") == pytest.approx(0.75)
    assert await ledger.spent_today("app-b") == pytest.approx(1.0)
    assert await ledger.spent_today("never-seen") == 0.0


async def test_sqlite_ledger_survives_a_restart(tmp_path) -> None:
    """A daily cap that resets on every restart protects nothing (§6.1)."""
    db = str(tmp_path / "jobs.db")

    first = SqliteCostLedger(db)
    await first.start()
    await first.add("app-a", 0.4)
    await first.add("app-a", 0.35)
    await first.close()

    second = SqliteCostLedger(db)
    await second.start()
    try:
        assert await second.spent_today("app-a") == pytest.approx(0.75)
    finally:
        await second.close()


async def test_sqlite_ledger_separates_days(tmp_path) -> None:
    ledger = SqliteCostLedger(str(tmp_path / "jobs.db"))
    await ledger.start()
    try:
        await ledger.add("app-a", 1.0)
        # A row from another day must not count towards today's total.
        await ledger._conn.execute(
            "INSERT INTO cost_ledger (label, day, cost_usd) VALUES (?, ?, ?)",
            ("app-a", "1999-01-01", 500.0),
        )
        await ledger._conn.commit()
        assert await ledger.spent_today("app-a") == pytest.approx(1.0)
        assert utc_day() != "1999-01-01"
    finally:
        await ledger.close()


async def test_a_ledger_that_cannot_open_degrades_to_no_accounting(tmp_path) -> None:
    """Better no cap than no API: an unusable ledger reports zero and logs."""
    ledger = SqliteCostLedger(str(tmp_path))  # a directory, not a file
    await ledger.start()
    await ledger.add("app-a", 1.0)
    assert await ledger.spent_today("app-a") == 0.0


def _override(ledger) -> None:
    app.dependency_overrides[get_searxng] = lambda: FakeSearxng()
    app.dependency_overrides[get_crawler] = lambda: FakeCrawler()
    app.dependency_overrides[get_llm] = lambda: FakeLLM(feedback_questions=[])
    app.dependency_overrides[get_reranker] = lambda: NoopReranker()
    app.dependency_overrides[get_cost_ledger] = lambda: ledger


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_a_key_over_its_daily_cap_gets_a_429(monkeypatch, tmp_path, backend) -> None:
    import anyio

    ledger = build_cost_ledger(backend, str(tmp_path / "jobs.db"))

    async def seed() -> None:
        await ledger.start()
        await ledger.add("anonymous", 5.0)

    anyio.run(seed)

    monkeypatch.setenv("DAILY_COST_CAP_USD", "1.0")
    get_settings.cache_clear()
    _override(ledger)
    try:
        with TestClient(app) as client:
            resp = client.post("/v1/research", json={"query": "x", "depth": 1, "breadth": 1})
            assert resp.status_code == 429
            assert "Daily cost cap" in resp.json()["detail"]
            # The same cap covers the other spending endpoint.
            assert client.post("/v1/answer", json={"query": "x"}).status_code == 429
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_a_key_under_its_cap_is_untouched(monkeypatch, tmp_path) -> None:
    """The acceptance criterion: one key capped, another still working."""
    import anyio

    ledger = build_cost_ledger("sqlite", str(tmp_path / "jobs.db"))

    async def seed() -> None:
        await ledger.start()
        await ledger.add("greedy", 99.0)

    anyio.run(seed)

    monkeypatch.setenv("DAILY_COST_CAP_USD", "1.0")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    key_greedy = "sk_" + "g" * 32
    key_thrifty = "sk_" + "t" * 32
    monkeypatch.setenv("API_KEYS", f"greedy:{key_greedy},thrifty:{key_thrifty}")
    get_settings.cache_clear()
    _override(ledger)
    try:
        with TestClient(app) as client:
            body = {"query": "x", "depth": 1, "breadth": 1, "interactive": False}
            greedy = client.post("/v1/research", json=body, headers={"X-API-Key": key_greedy})
            thrifty = client.post("/v1/research", json=body, headers={"X-API-Key": key_thrifty})
        assert greedy.status_code == 429
        assert thrifty.status_code == 200
        assert thrifty.json()["status"] == "completed"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_the_cap_is_off_by_default(monkeypatch, tmp_path) -> None:
    import anyio

    ledger = InMemoryCostLedger()
    anyio.run(ledger.add, "anonymous", 1_000.0)
    _override(ledger)
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research", json={"query": "x", "depth": 1, "breadth": 1, "interactive": False}
            )
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_a_completed_request_is_billed_to_its_label(monkeypatch) -> None:
    import anyio

    ledger = InMemoryCostLedger()

    class BilledLLM(FakeLLM):
        async def structured(self, messages, response_model, *, model=None, usage=None, **kwargs):
            if usage is not None:
                usage.total_cost_usd += 0.01
            return await super().structured(messages, response_model, model=model, **kwargs)

        async def complete(self, messages, *, usage=None, **kwargs) -> str:
            if usage is not None:
                usage.total_cost_usd += 0.01
            return await super().complete(messages, **kwargs)

    _override(ledger)
    app.dependency_overrides[get_llm] = lambda: BilledLLM(feedback_questions=[])
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research", json={"query": "x", "depth": 1, "breadth": 1, "interactive": False}
            )
        assert resp.status_code == 200
        assert resp.json()["usage"]["total_cost_usd"] > 0
        assert anyio.run(ledger.spent_today, "anonymous") > 0
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


# -- c) estimate_only -------------------------------------------------------


def test_fanout_matches_the_engines_recursion() -> None:
    # depth=1: just `breadth` queries, one node.
    assert _total_queries(4, 1) == (4, 1)
    # depth=2: 4 queries, each recursing at ceil(4/2)=2 -> 4 + 4*2 = 12.
    assert _total_queries(4, 2) == (12, 5)
    assert _total_queries(0, 3) == (0, 0)
    assert _total_queries(4, 0) == (0, 0)


def test_estimate_scales_with_the_request() -> None:
    kwargs = dict(
        model="openai/gpt-4o-mini",
        crawl_top_k=3,
        process_context_token_budget=8000,
        report_token_budget=12000,
        rerank_enabled=True,
    )
    small = estimate_run(breadth=2, depth=1, **kwargs)
    large = estimate_run(breadth=8, depth=3, **kwargs)

    assert large["serp_queries"] > small["serp_queries"]
    assert large["pages_crawled"] == large["serp_queries"] * 3
    assert large["estimated_cost_usd"] > small["estimated_cost_usd"] > 0


def test_an_unknown_model_is_still_priced() -> None:
    estimate = estimate_run(
        breadth=2,
        depth=1,
        model="some-vendor/not-a-real-model",
        crawl_top_k=3,
        process_context_token_budget=8000,
        report_token_budget=12000,
        rerank_enabled=False,
    )
    assert estimate["estimated_cost_usd"] > 0


class ExplodingClient:
    """Any use at all is a test failure: `estimate_only` must not touch these."""

    def __getattr__(self, name):
        raise AssertionError(f"estimate_only must not call {name}()")


def test_estimate_only_launches_nothing() -> None:
    app.dependency_overrides[get_searxng] = lambda: ExplodingClient()
    app.dependency_overrides[get_crawler] = lambda: ExplodingClient()
    app.dependency_overrides[get_llm] = lambda: ExplodingClient()
    app.dependency_overrides[get_reranker] = lambda: ExplodingClient()
    try:
        with TestClient(app) as client:
            resp = client.post(
                "/v1/research",
                json={"query": "x", "breadth": 4, "depth": 2, "estimate_only": True},
            )
        assert resp.status_code == 200
        body = resp.json()
        # Distinguishable from a real result at a glance.
        assert body["status"] == "estimated"
        assert body["report"] is None
        assert body["estimate"]["serp_queries"] == 12
        assert body["estimate"]["pages_crawled"] > 0
        assert body["estimate"]["estimated_cost_usd"] > 0
    finally:
        app.dependency_overrides.clear()
