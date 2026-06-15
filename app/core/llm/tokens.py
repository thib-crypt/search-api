"""Lightweight token counting / trimming used to bound LLM context size.

Uses tiktoken's ``o200k_base`` (GPT-4o family) as a provider-agnostic
approximation. Exact counts differ per provider, but it is good enough to keep
prompts within budget.
"""

from __future__ import annotations

import tiktoken

_encoding: tiktoken.Encoding | None = None


def _enc() -> tiktoken.Encoding:
    global _encoding
    if _encoding is None:
        try:
            _encoding = tiktoken.get_encoding("o200k_base")
        except Exception:  # pragma: no cover - fallback for older tiktoken
            _encoding = tiktoken.get_encoding("cl100k_base")
    return _encoding


def count_tokens(text: str) -> int:
    return len(_enc().encode(text))


def trim_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    enc = _enc()
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return enc.decode(tokens[:max_tokens])
