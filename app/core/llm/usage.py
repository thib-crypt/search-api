"""Token / cost accounting for one logical unit of work (§6.5).

One :class:`UsageTracker` is created per ``/v1/research`` run and per
``/v1/answer`` call — never shared process-wide. It is threaded through the LLM
client the same way ``on_progress`` / ``on_token`` are threaded through the
engine: an optional keyword argument that costs nothing when it is absent.

Two rules govern everything here:

* **accounting must never break the thing it measures.** Every lookup into a
  provider response is best-effort; a provider that omits ``usage`` (common on
  streaming responses) contributes nothing rather than raising. A billing
  nice-to-have that can fail a ten-minute research run is worse than no billing
  at all.
* **the numbers are advisory.** litellm's cost map does not know every model, so
  ``total_cost_usd`` is a good estimate, not an invoice. The circuit breakers of
  §6.4 are sized with that in mind.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import litellm

logger = logging.getLogger("unified_search_api.usage")


@dataclass
class UsageTracker:
    """Running totals for one research run or one answer call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_cost_usd: float = 0.0
    searches: int = 0
    pages_crawled: int = 0
    pages_skipped_by_rerank: int = 0

    def record_completion(self, response: Any) -> None:
        """Fold a litellm response (or a final stream chunk) into the totals.

        Never raises. A response without ``usage`` adds zero tokens; a model
        litellm cannot price adds zero cost.
        """
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.prompt_tokens += _as_int(usage, "prompt_tokens")
            self.completion_tokens += _as_int(usage, "completion_tokens")

        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception as exc:  # noqa: BLE001 - pricing is advisory, never fatal
            logger.debug("Cost lookup failed (model not in litellm's cost map?): %s", exc)
            return
        if isinstance(cost, int | float) and cost > 0:
            self.total_cost_usd += float(cost)

    def record_search(self, count: int = 1) -> None:
        self.searches += count

    def record_pages_crawled(self, count: int) -> None:
        self.pages_crawled += max(count, 0)

    def record_pages_skipped(self, count: int) -> None:
        self.pages_skipped_by_rerank += max(count, 0)

    def snapshot(self) -> dict[str, Any]:
        """A plain dict, safe to embed in a JSON response or a job result."""
        data = asdict(self)
        # Sub-cent runs are the common case; more precision than this is noise.
        data["total_cost_usd"] = round(self.total_cost_usd, 6)
        return data


def _as_int(usage: Any, field: str) -> int:
    value = getattr(usage, field, None)
    if value is None and isinstance(usage, dict):  # some providers hand back a plain dict
        value = usage.get(field)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def record(usage: UsageTracker | None, response: Any) -> None:
    """``usage.record_completion(response)`` when a tracker is present.

    Keeps the call sites in `LLMClient` free of ``if usage is not None`` noise.
    """
    if usage is not None:
        usage.record_completion(response)
