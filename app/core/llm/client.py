"""Provider-agnostic LLM client built on litellm + instructor.

A single client serves every provider; the model is chosen per call using a
litellm-style string (``openai/gpt-4o-mini``, ``anthropic/claude-...``,
``gemini/...``, ``openrouter/...``). Provider keys are read from the environment
by litellm. ``complete`` returns plain text; ``structured`` returns a validated
Pydantic model.
"""

from __future__ import annotations

from typing import TypeVar

import instructor
import litellm
from litellm import acompletion
from pydantic import BaseModel

# Drop params a given provider doesn't support instead of erroring (e.g. some
# models reject `temperature`).
litellm.drop_params = True

T = TypeVar("T", bound=BaseModel)

Message = dict[str, str]


class LLMClient:
    def __init__(
        self, default_model: str, temperature: float = 0.2, max_tokens: int = 4096
    ) -> None:
        self.default_model = default_model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._aclient = instructor.from_litellm(acompletion)

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        resp = await acompletion(
            model=model or self.default_model,
            messages=messages,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=max_tokens or self.max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()

    async def structured(
        self,
        messages: list[Message],
        response_model: type[T],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_retries: int = 2,
    ) -> T:
        return await self._aclient.create(
            model=model or self.default_model,
            messages=messages,
            response_model=response_model,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=max_tokens or self.max_tokens,
            max_retries=max_retries,
        )
