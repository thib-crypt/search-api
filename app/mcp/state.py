"""The shared clients an MCP tool needs, read off `app.state`.

Every object here is the *same instance* the HTTP routes use — that is the whole
point of running MCP in-process (§0). One browser pool honouring
`CRAWL_MAX_CONCURRENCY`, one SearXNG cache to hit, one cost ledger enforcing one
daily cap, one `JobManager` whose ids work on both surfaces.

Wrapped in typed properties rather than read attribute-by-attribute inside each
tool for two reasons. `app.state` is untyped, so `mypy --strict` would have
nothing to check and a renamed field would surface as an `AttributeError` on the
first production call rather than at startup. And the properties **read through**
on every access instead of snapshotting: a frozen copy taken at build time would
pin whatever client existed then, which is wrong the moment anything replaces one
— a reconnect, or a test that installs a fake after the app has started.
"""

from __future__ import annotations

from typing import Any

from app.core.cache import TTLCache
from app.core.cost import CostLedger
from app.core.crawl.crawler import CrawlerService
from app.core.llm.client import LLMClient
from app.core.net.guard import UrlGuard
from app.core.research.rerank import Reranker
from app.core.search.searxng import SearxngClient
from app.jobs.manager import JobManager
from app.mcp.artifacts import ArtifactStore
from app.mcp.guards import ConcurrencyBound
from app.models.answer import AnswerResponse

# Everything a tool may reach for. Checked once, at build time, so a wiring
# mistake aborts startup instead of failing the first call that needed it.
_REQUIRED = (
    "searxng",
    "crawler",
    "llm",
    "reranker",
    "url_guard",
    "jobs",
    "cost_ledger",
    "mcp_artifacts",
    "mcp_research_bound",
)


class McpState:
    """Typed, live view onto the application state."""

    def __init__(self, app_state: Any) -> None:
        self._state = app_state

    @property
    def searxng(self) -> SearxngClient:
        return self._state.searxng  # type: ignore[no-any-return]

    @property
    def crawler(self) -> CrawlerService:
        return self._state.crawler  # type: ignore[no-any-return]

    @property
    def llm(self) -> LLMClient:
        return self._state.llm  # type: ignore[no-any-return]

    @property
    def reranker(self) -> Reranker:
        return self._state.reranker  # type: ignore[no-any-return]

    @property
    def url_guard(self) -> UrlGuard:
        return self._state.url_guard  # type: ignore[no-any-return]

    @property
    def jobs(self) -> JobManager:
        return self._state.jobs  # type: ignore[no-any-return]

    @property
    def cost_ledger(self) -> CostLedger:
        return self._state.cost_ledger  # type: ignore[no-any-return]

    @property
    def artifacts(self) -> ArtifactStore:
        return self._state.mcp_artifacts  # type: ignore[no-any-return]

    @property
    def research_bound(self) -> ConcurrencyBound:
        return self._state.mcp_research_bound  # type: ignore[no-any-return]

    @property
    def answer_cache(self) -> TTLCache[AnswerResponse] | None:
        cache: TTLCache[AnswerResponse] | None = getattr(self._state, "answer_cache", None)
        return cache


def state_of(app_state: Any) -> McpState:
    """Wrap an ``app.state``-like object, refusing one that is not ready yet."""
    missing = [name for name in _REQUIRED if not hasattr(app_state, name)]
    if missing:  # pragma: no cover - a wiring bug, caught at startup
        raise RuntimeError(
            f"The MCP server was built before the API clients were: {', '.join(missing)} "
            "missing from app.state. build_server() must run inside the application "
            "lifespan, once app.state has been populated."
        )
    return McpState(app_state)
