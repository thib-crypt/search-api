"""Projected cost of a research run that has not been launched (§6.4c).

`estimate_only: true` answers "what would this cost?" without a single search,
crawl or model call — so a client can decide before committing, and so a budget
owner can price a `breadth=10, depth=5` request before someone runs it.

**Method.** The fanout is exact: it replays the engine's own recursion (each of
the `breadth` queries recurses at `ceil(breadth/2)` for `depth-1` levels) to get
the total number of SERP queries. Everything downstream is per-query arithmetic:
one search, one rerank call, `crawl_top_k` pages, one extraction call — plus one
query-generation call per recursion node and one report at the end.

The *token* figures are averages, not measurements: the extraction call is
budgeted at the configured context budget, the report at its learnings budget,
and outputs at rough observed sizes. Pricing comes from litellm's model cost map
when it knows the model, and from a conservative fallback rate when it does not.

Expect the result to be right to within a factor of about two. That is enough to
answer "cents or dollars?", which is the question it exists to answer — and it
is why the response calls the field `estimated_cost_usd`.
"""

from __future__ import annotations

import logging
import math
from typing import TypedDict

import litellm

logger = logging.getLogger("unified_search_api.research.estimate")

# Average prompt/completion tokens per call type, per SERP query.
_RERANK_PROMPT = 800
_RERANK_COMPLETION = 100
_SERP_GEN_PROMPT = 600
_SERP_GEN_COMPLETION = 300
_PROCESS_COMPLETION = 500
_REPORT_COMPLETION = 2500

# Used when litellm cannot price the model. Roughly a mid-tier model's rate, so
# an unknown model is over-estimated rather than sold as free.
_FALLBACK_INPUT_PER_TOKEN = 0.5e-6
_FALLBACK_OUTPUT_PER_TOKEN = 1.5e-6


def _total_queries(breadth: int, depth: int) -> tuple[int, int]:
    """(SERP queries, recursion nodes) for a run of this shape.

    Mirrors `DeepResearchEngine._deep_research`: a node emits `breadth` queries
    and *each* of them opens a child node at half the breadth, one level down.
    """
    if depth <= 0 or breadth <= 0:
        return 0, 0
    child_queries, child_nodes = _total_queries(math.ceil(breadth / 2), depth - 1)
    return breadth + breadth * child_queries, 1 + breadth * child_nodes


def _rates(model: str) -> tuple[float, float]:
    """(input, output) USD per token for ``model``, with a fallback."""
    try:
        entry = litellm.model_cost.get(model) or litellm.model_cost.get(model.split("/", 1)[-1])
    except Exception:  # noqa: BLE001 - an estimate must never raise
        entry = None
    if not entry:
        logger.debug("No cost entry for %r; estimating at the fallback rate", model)
        return _FALLBACK_INPUT_PER_TOKEN, _FALLBACK_OUTPUT_PER_TOKEN
    return (
        float(entry.get("input_cost_per_token") or _FALLBACK_INPUT_PER_TOKEN),
        float(entry.get("output_cost_per_token") or _FALLBACK_OUTPUT_PER_TOKEN),
    )


class RunEstimate(TypedDict):
    """Exactly the fields of `app.models.research.ResearchEstimate`.

    Declared so the route can unpack it into the response model and have that
    unpack type-checked, instead of shipping a `dict[str, float | int | str]`
    that no checker can match against the model's signature.
    """

    serp_queries: int
    searches: int
    pages_crawled: int
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    model: str


def estimate_run(
    *,
    breadth: int,
    depth: int,
    model: str,
    crawl_top_k: int,
    process_context_token_budget: int,
    report_token_budget: int,
    rerank_enabled: bool,
) -> RunEstimate:
    """Project the cost of a run. Pure arithmetic — no network, no model call."""
    queries, nodes = _total_queries(max(breadth, 0), max(depth, 0))
    pages = queries * max(crawl_top_k, 0)

    prompt_tokens = (
        nodes * _SERP_GEN_PROMPT
        + queries * process_context_token_budget
        + (queries * _RERANK_PROMPT if rerank_enabled else 0)
        + report_token_budget
    )
    completion_tokens = (
        nodes * _SERP_GEN_COMPLETION
        + queries * _PROCESS_COMPLETION
        + (queries * _RERANK_COMPLETION if rerank_enabled else 0)
        + _REPORT_COMPLETION
    )
    llm_calls = nodes + queries + (queries if rerank_enabled else 0) + 1

    input_rate, output_rate = _rates(model)
    cost = prompt_tokens * input_rate + completion_tokens * output_rate

    return {
        "serp_queries": queries,
        "searches": queries,
        "pages_crawled": pages,
        "llm_calls": llm_calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": round(cost, 6),
        "model": model,
    }
