"""What the HTTP routes get from dependencies, re-applied per tool call (§6).

`app.mcp.server` explains why this is necessary at all: a mounted ASGI app runs
the app's middleware but never its router dependencies, so everything the
`APIRouter(dependencies=[...])` lines and the per-route `Depends` provide has to
be asked for by name on this side. Three things, and one that is new:

* **the caller's label** — from the verified bearer token, not from a header, so
  it is the same label the HTTP side would have attributed the spend to;
* **the rate-limit top-up** — the limiter priced this request from its path
  before routing, i.e. at 1 point for every tool alike; the difference between
  that and what the tool is actually worth is debited here (§6.2);
* **the daily cost cap** — checked before the spending starts, refused with a
  message that says how much of the budget is gone (§6.3);
* **a concurrency bound per label** — new, and specific to agents. A person runs
  one `deep_research` and waits for it. A loop can start twenty in a minute and
  reach the daily cap before anyone notices, so `MCP_RESEARCH_MAX_CONCURRENT`
  caps the simultaneous runs one key can have in flight.

Refusals are raised as `ToolRefused`, which the SDK turns into a tool result
flagged `is_error`. That is the right channel: the model *sees* the text and can
act on it, where a protocol-level error is something the client swallows. So the
messages here state the number, the budget and the delay — everything an agent
needs to back off correctly instead of retrying into the same wall.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context

from app.api.middleware import bucket_key, charge, path_weight
from app.config import ANONYMOUS_LABEL, Settings, get_settings
from app.core import metrics
from app.core.cost import CostLedger

logger = logging.getLogger("unified_search_api.mcp")

# §6.2. Aligned with `_WEIGHTS` in app/api/middleware.py: the same work costs the
# same points whether it was asked for over HTTP or over MCP. `job_status` and
# `deep_research_estimate` are polling/pricing calls that spend nothing, so they
# stay at the default weight the middleware already charged.
TOOL_WEIGHTS: dict[str, int] = {
    "web_search": 1,
    "map_site": 2,
    "fetch_url": 5,
    "answer": 10,
    "extract_structured": 10,
    "crawl_site": 25,
    "deep_research": 100,
    "deep_research_estimate": 1,
    "job_status": 1,
}


class ToolRefused(Exception):
    """A call turned away before it did any work. The message is for the agent."""


def caller_label() -> str:
    """The API-key label behind this call, or ``anonymous``.

    Reads the token the `ApiKeyVerifier` validated rather than a header: by this
    point the key is gone and only the label remains, which is the invariant the
    rest of the codebase relies on.
    """
    token = get_access_token()
    client_id = getattr(token, "client_id", None)
    return client_id or ANONYMOUS_LABEL


def _request_scope(ctx: Context[Any, Any]) -> dict[str, Any] | None:
    """The ASGI scope of the HTTP request carrying this call, when there is one.

    ``None`` under the stdio transport, which has no HTTP request at all — the
    rate limiter is a property of the HTTP surface, and a desktop client talking
    over a pipe is not sharing anyone's budget.
    """
    try:
        request = getattr(ctx.request_context, "request", None)
    except (ValueError, LookupError):  # pragma: no cover - no request context
        return None
    scope = getattr(request, "scope", None)
    return scope if isinstance(scope, dict) else None


def charge_tool(ctx: Context[Any, Any], tool: str, settings: Settings) -> None:
    """Debit what ``tool`` really costs, minus what the middleware already took.

    The subtraction is the point. `RateLimitMiddleware` charged `path_weight("/mcp")`
    on the way in — 1 point, because it runs before routing and has only a path to
    price. Charging the full tool weight here would bill `deep_research` 101.
    """
    scope = _request_scope(ctx)
    if scope is None:
        return
    weight = TOOL_WEIGHTS.get(tool, 1)
    already_charged = path_weight(str(scope.get("path", "")))
    extra = weight - already_charged
    if extra <= 0:
        return
    label = caller_label()
    decision = charge(bucket_key(scope, settings), extra, label=label)
    if not decision.allowed:
        raise ToolRefused(
            f"Rate limit exceeded: {tool} costs {weight} points and only "
            f"{decision.remaining} of {decision.limit} remain in the current "
            f"60s window. Retry in {decision.reset_seconds}s, or use a cheaper "
            f"tool — see search-api://capabilities for the per-tool costs."
        )


async def check_cost_cap(ledger: CostLedger, label: str, settings: Settings) -> None:
    """Refuse a spending tool once its key is over budget for the UTC day (§6.3).

    Same coarse contract as `enforce_daily_cost_cap`: checked before any work, so
    a key can overshoot by at most the run that was already under way when it
    crossed. Tightening that would mean pricing every run up front, which is what
    `deep_research_estimate` is for.
    """
    cap = settings.daily_cost_cap_usd
    if cap <= 0:
        return
    spent = await ledger.spent_today(label)
    if spent >= cap:
        raise ToolRefused(
            f"Daily cost cap reached for this API key: ${spent:.4f} spent of "
            f"${cap:.2f} allowed. The budget resets at 00:00 UTC. Free tools "
            "(web_search, map_site, fetch_url, job_status) still work."
        )


@dataclass
class ConcurrencyBound:
    """Per-label cap on simultaneous runs of one expensive tool (§6.3).

    Acquire and release are separate calls rather than a context manager because
    the thing being bounded outlives the call that started it: `deep_research`
    returns a job id in milliseconds and the run it started keeps going for
    minutes. A `with` block around the tool body would release the slot the
    instant the id was handed back, which bounds nothing at all. The job task
    releases it in a `finally` instead.
    """

    limit: int
    _active: dict[str, int] = field(default_factory=dict)

    def active(self, label: str) -> int:
        return self._active.get(label, 0)

    def acquire(self, label: str, tool: str) -> None:
        if self.limit <= 0:
            return
        current = self._active.get(label, 0)
        if current >= self.limit:
            raise ToolRefused(
                f"This API key already has {current} {tool} run(s) in flight, which "
                f"is the per-key limit ({self.limit}). Collect one with job_status "
                "before starting another."
            )
        self._active[label] = current + 1

    def release(self, label: str) -> None:
        if self.limit <= 0:
            return
        remaining = self._active.get(label, 1) - 1
        if remaining > 0:
            self._active[label] = remaining
        else:
            self._active.pop(label, None)


@dataclass
class ToolCall:
    """Bookkeeping for one tool call: who, how long, and how big the result was."""

    tool: str
    label: str
    started: float
    result_tokens: int | None = None


@asynccontextmanager
async def tool_call(
    ctx: Context[Any, Any], tool: str, settings: Settings
) -> AsyncIterator[ToolCall]:
    """Charge, time and count one tool call.

    Wraps the body of every tool so the rate-limit top-up cannot be forgotten on
    a new one — the failure mode of §6.2 is silent, and a tool that skipped the
    debit would look perfectly healthy while costing a hundredth of what it
    should.
    """
    label = caller_label()
    call = ToolCall(tool=tool, label=label, started=time.perf_counter())
    outcome = "success"
    try:
        charge_tool(ctx, tool, settings)
        yield call
    except ToolRefused:
        outcome = "refused"
        raise
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        metrics.record_mcp_call(tool, label, outcome, time.perf_counter() - call.started)
        if call.result_tokens is not None:
            metrics.record_mcp_result_tokens(tool, call.result_tokens)


def settings_for_call() -> Settings:
    """Settings, re-read per call like every handler in `app.api.routes` does."""
    return get_settings()
