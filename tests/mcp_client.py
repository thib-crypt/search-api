"""An MCP client session against the app, over its real ASGI stack.

Deliberately not an in-memory server harness. Half of what Phase 14 had to get
right lives in the HTTP layer — the bearer-token check, the weighted rate-limit
window, the exact-path route that avoids a 307 — and a harness that spoke to the
server object directly would exercise none of it. These tests go through
`app.main:app`, so a regression in the mount or in the auth wiring fails them.

The transport client is `httpx2` (the SDK's HTTP stack, shipped alongside the
`httpx` 0.28 the rest of the app uses), pointed at an in-process ASGI transport.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
from asgi_lifespan import LifespanManager
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

BASE_URL = "http://testserver"


@asynccontextmanager
async def mcp_session(
    app: Any,
    *,
    api_key: str | None = None,
    path: str = "/mcp",
    elicitation_callback: Any = None,
) -> AsyncIterator[ClientSession]:
    """An initialized MCP session against ``app``, with its lifespan running.

    Passing ``elicitation_callback`` is what makes the client *declare* the
    elicitation capability, which is the branch §5 turns on.
    """
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(LifespanManager(app))
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), headers=headers, timeout=30.0
        )
        read, write = await stack.enter_async_context(
            streamable_http_client(f"{BASE_URL}{path}", http_client=client)
        )
        session = await stack.enter_async_context(
            ClientSession(read, write, elicitation_callback=elicitation_callback)
        )
        await session.initialize()
        yield session


@asynccontextmanager
async def live_mcp_session(app: Any, *, elicitation_callback: Any = None) -> AsyncIterator[Any]:
    """A session over a real socket, for the paths that need real streaming.

    `httpx2.ASGITransport` buffers a response body to completion before handing
    it back, which is fine for the request/response half of streamable HTTP and
    fatal for the other half: a server→client request (elicitation) travels down
    the SSE stream of the very call that is waiting for its answer, so an
    in-memory transport deadlocks on itself. Nothing is wrong with the server —
    it is the test transport that cannot stream — so those tests get a real
    uvicorn on an ephemeral port instead.
    """
    import asyncio

    import uvicorn
    from sse_starlette.sse import AppStatus

    # sse_starlette drains every SSE stream in the process once *any* uvicorn
    # server has been asked to exit — `AppStatus.should_exit` is a module global,
    # which is correct for a deployment (one server, one process) and wrong for a
    # test file that starts several in a row: the second server would answer
    # every streamed request with an empty body. Reset it per server.
    AppStatus.should_exit = False

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if serving.done():  # pragma: no cover - startup crash
                await serving
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]

        async with AsyncExitStack() as stack:
            client = httpx2.AsyncClient(timeout=30.0)
            read, write = await stack.enter_async_context(
                streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client)
            )
            session = await stack.enter_async_context(
                ClientSession(read, write, elicitation_callback=elicitation_callback)
            )
            await session.initialize()
            yield session
    finally:
        server.should_exit = True
        await serving


@asynccontextmanager
async def raw_client(app: Any) -> AsyncIterator[httpx2.AsyncClient]:
    """A plain HTTP client against ``app``, for the pre-session checks (401, 404)."""
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(LifespanManager(app))
        client = await stack.enter_async_context(
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE_URL)
        )
        yield client


class StubResearchEngine:
    """A `DeepResearchEngine` stand-in: one clarifying question, instant report.

    Shared by every test that starts a `deep_research`, so none of them lets a
    background job reach litellm — a run that fails after three provider retries
    is thirty seconds of test time and a leaked coroutine warning, for a code
    path the test was not looking at.
    """

    def __init__(self, questions: list[str] | None = None, run_seconds: float = 0.0) -> None:
        self.questions = (
            ["Which database version are you targeting?"] if questions is None else questions
        )
        # A run that returns instantly releases its concurrency slot instantly,
        # which is exactly what the per-key bound is not about. Tests that care
        # keep one in flight.
        self.run_seconds = run_seconds
        self.queries: list[str] = []

    async def generate_feedback(self, query: str, **kwargs: Any) -> list[str]:
        return list(self.questions)

    async def run(self, query: str, **kwargs: Any) -> Any:
        import asyncio

        from app.core.research.engine import ResearchResult

        self.queries.append(query)
        if self.run_seconds:
            await asyncio.sleep(self.run_seconds)
        return ResearchResult(report="done", learnings=[], sources=[], serp_queries=[])


def install_stub_engine(monkeypatch: Any, **kwargs: Any) -> StubResearchEngine:
    engine = StubResearchEngine(**kwargs)
    monkeypatch.setattr("app.mcp.tools._engine", lambda state, settings: engine)
    return engine


def text_of(result: CallToolResult) -> str:
    """Every text block of a tool result, concatenated."""
    return "\n".join(block.text for block in result.content if block.type == "text")


def resource_links(result: CallToolResult) -> list[str]:
    return [str(block.uri) for block in result.content if block.type == "resource_link"]
