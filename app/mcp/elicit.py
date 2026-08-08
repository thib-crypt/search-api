"""Clarifying questions, asked through the client when it can ask (§5).

`POST /v1/research` can come back with `status: "needs_clarification"` and a list
of questions; the HTTP client is expected to call again with `answers`. MCP has
something better for the same exchange: `elicitation/create`, where the *server*
asks the *client* to put a form in front of the user.

The difference matters for a broad research question. Over HTTP, the round trip
is the caller's problem. Over MCP with elicitation, the answers come back inside
the same tool call, so the agent never sees a half-failed call it has to
understand and retry — it sees a `deep_research` that worked.

Three paths, and all three end with the run going ahead:

* client declares `elicitation` → a one-string-field-per-question form, answers
  folded into the query;
* client does not → the questions come back as the tool result, with an explicit
  instruction to call again with `answers`. This is the HTTP behaviour;
* user declines or cancels → the run starts unclarified, which is what
  `interactive: false` does on the HTTP side. Never a dead end: the user asked
  for research, and refusing to elaborate is not a refusal to research.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger("unified_search_api.mcp")

# Elicitation forms are flat objects of primitives by design, so a question
# becomes one required string field. More than a handful of them stops being a
# form and starts being an interrogation.
_MAX_QUESTIONS = 5


def _form_model(questions: list[str]) -> type[BaseModel]:
    fields: dict[str, Any] = {
        f"answer_{index}": (
            str,
            Field(default="", description=question),
        )
        for index, question in enumerate(questions, start=1)
    }
    return create_model("ResearchClarification", **fields)


def supports_elicitation(ctx: Context[Any, Any]) -> bool:
    capabilities = ctx.client_capabilities
    return bool(capabilities is not None and getattr(capabilities, "elicitation", None))


async def resolve_clarification(
    ctx: Context[Any, Any], query: str, questions: list[str]
) -> list[str] | None:
    """Answers to ``questions``, or ``None`` when the caller must supply them.

    ``None`` means "hand the questions back to the agent" — the fallback path.
    An empty list means "go ahead without clarification", which is what a
    declined or cancelled form means: the user was asked and chose not to say
    more, so the run proceeds as if it had never been interactive.
    """
    asked = questions[:_MAX_QUESTIONS]
    if not asked or not supports_elicitation(ctx):
        return None

    message = (
        f"Before starting a deep research run on {query!r} — which takes minutes "
        "and costs real money — a few details would narrow it down:"
    )
    try:
        result = await ctx.elicit(message, _form_model(asked))
    except Exception:  # noqa: BLE001 - a client that mishandles the form must not
        # sink the whole call; fall back to returning the questions as text.
        logger.info("Elicitation failed; falling back to returning the questions", exc_info=True)
        return None

    action = getattr(result, "action", None)
    if action != "accept":
        # Declined or cancelled: research the query as it stands.
        return []
    data = getattr(result, "data", None)
    if data is None:  # pragma: no cover - defensive; accept implies data
        return []
    answers = [
        f"{question} {value.strip()}"
        for question, value in zip(asked, _values(data, len(asked)), strict=False)
        if value.strip()
    ]
    return answers


def _values(data: BaseModel, count: int) -> list[str]:
    return [str(getattr(data, f"answer_{index}", "") or "") for index in range(1, count + 1)]
