"""Provider-agnostic LLM client built on litellm + instructor.

A single client serves every provider; the model is chosen per call using a
litellm-style string (``openai/gpt-4o-mini``, ``anthropic/claude-...``,
``gemini/...``, ``openrouter/...``). Provider keys are read from the environment
by litellm. ``complete`` returns plain text; ``structured`` returns a validated
Pydantic model.

Every call goes through a ``litellm.Router`` rather than ``acompletion`` directly:
the router owns the timeouts, the retry policy and the fallback chain, so a single
provider 429 or a hung connection no longer kills a ten-minute research job.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from typing import Any, TypeVar

import instructor
import litellm
from litellm.router import Router
from litellm.types.router import (
    AllowedFailsPolicy,
    Deployment,
    LiteLLM_Params,
    RetryPolicy,
)
from pydantic import BaseModel

# Drop params a given provider doesn't support instead of erroring (e.g. some
# models reject `temperature`).
litellm.drop_params = True

T = TypeVar("T", bound=BaseModel)

Message = dict[str, str]

# Process-wide cap on in-flight LLM calls. Retries and fallbacks multiply the
# requests a single logical call can make, so the guard is taken around the whole
# call rather than per provider request. Keyed by event loop because an
# `asyncio.Semaphore` binds to the loop that first awaits it (a weak key so a
# closed loop doesn't keep its semaphore alive).
_GUARDS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _concurrency_guard(limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    guard = _GUARDS.get(loop)
    if guard is None:
        guard = asyncio.Semaphore(limit)
        _GUARDS[loop] = guard
    return guard


def _deployment(model: str) -> dict[str, Any]:
    """One router deployment per model, named after the model string itself."""
    return {"model_name": model, "litellm_params": {"model": model}}


class LLMClient:
    def __init__(
        self,
        default_model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        fallback_models: list[str] | None = None,
        timeout: float = 120.0,
        stream_timeout: float = 30.0,
        max_retries: int = 3,
        max_concurrency: int = 10,
    ) -> None:
        self.default_model = default_model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # A cap of 0 would make the guard block forever; clamp like CrawlerService.
        self.max_concurrency = max(max_concurrency, 1)
        fallbacks = [m for m in (fallback_models or []) if m and m != default_model]
        self._router = Router(
            model_list=[_deployment(m) for m in (default_model, *fallbacks)],
            # Fallbacks hang off the primary model group only: a caller-supplied
            # model is an explicit choice and shouldn't be silently swapped.
            fallbacks=[{default_model: fallbacks}] if fallbacks else [],
            num_retries=max_retries,
            timeout=timeout,
            stream_timeout=stream_timeout,
            retry_policy=RetryPolicy(
                RateLimitErrorRetries=3,
                TimeoutErrorRetries=2,
                # A bad key or a malformed request won't fix itself on retry.
                AuthenticationErrorRetries=0,
                BadRequestErrorRetries=1,
            ),
            # Rate limits are expected under load; don't cool a deployment down for them.
            allowed_fails_policy=AllowedFailsPolicy(RateLimitErrorAllowedFails=100),
        )
        self._aclient = instructor.from_litellm(self._acompletion)

    def _register(self, model: str) -> None:
        """Add a deployment on demand.

        Callers may name any litellm model per request (`/v1/answer` and
        `/v1/research` both accept one), and the router rejects models missing
        from its list.
        """
        if model in self._router.model_names:
            return
        self._router.upsert_deployment(
            Deployment(model_name=model, litellm_params=LiteLLM_Params(model=model))
        )

    async def _acompletion(self, **kwargs: Any) -> Any:
        """Router-backed stand-in for ``litellm.acompletion``.

        Also what instructor patches, so structured calls get the same retries,
        timeouts and fallbacks as the plain ones. The concurrency guard is taken
        by the public methods, not here, so instructor's own re-asks don't try to
        re-acquire a semaphore the task already holds.
        """
        model = kwargs.get("model") or self.default_model
        kwargs["model"] = model
        self._register(model)
        return await self._router.acompletion(**kwargs)

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        async with _concurrency_guard(self.max_concurrency):
            resp = await self._acompletion(
                model=model or self.default_model,
                messages=messages,
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.max_tokens,
            )
        return (resp.choices[0].message.content or "").strip()

    async def stream_complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        # The guard is held for the whole stream: the provider connection stays
        # open until the last chunk, so the slot is genuinely in use until then.
        async with _concurrency_guard(self.max_concurrency):
            resp = await self._acompletion(
                model=model or self.default_model,
                messages=messages,
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.max_tokens,
                stream=True,
            )
            async for chunk in resp:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta

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
        async with _concurrency_guard(self.max_concurrency):
            return await self._aclient.create(
                model=model or self.default_model,
                messages=messages,
                response_model=response_model,
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.max_tokens,
                max_retries=max_retries,
            )
