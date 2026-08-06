"""Lightweight token counting / trimming used to bound LLM context size.

Uses tiktoken's ``o200k_base`` (GPT-4o family) as a provider-agnostic
approximation. Exact counts differ per provider, but it is good enough to keep
prompts within budget.

``enc.encode``/``enc.decode`` are CPU-bound and, on large markdown pages, can
take tens of milliseconds — long enough to freeze the single-worker asyncio
event loop. Both public functions are therefore async: a cheap character-based
pre-truncation skips most of the encode work up front, and the remaining
tiktoken call runs in a worker thread via ``asyncio.to_thread`` (tiktoken's
Rust core releases the GIL, so this gives real parallelism rather than just
deferring the block).
"""

from __future__ import annotations

import asyncio

import tiktoken

_encoding: tiktoken.Encoding | None = None

# First guess at characters-per-token for the o200k/cl100k encodings, used to
# slice the text before encoding it. Ordinary prose sits near 4; this leaves
# headroom.
#
# It is a *guess*, not a bound: BPE merges long runs of the same character into
# single tokens, so whitespace, `---` rules and deep indentation -- all of which
# crawled markdown is full of -- reach 16-128 characters per token. Slicing such
# text at `max_tokens * 6` characters yielded a fraction of the requested budget
# (60k spaces trimmed to 1000 tokens returned 48), silently starving the model of
# context. `_trim_sync` therefore *verifies* the slice and grows it when it comes
# up short, instead of trusting this number.
_CHARS_PER_TOKEN_GUESS = 6
# Factor the slice grows by on each retry.
_SLICE_GROWTH = 8
# Tokens the slice must overshoot the budget by before its prefix is trusted.
# Cutting the text mid-run can change how the *last* tokens of the slice merge
# (`'.' * 48` and `'.' * 60000` do not start with the same token), so the slice
# has to reach past the budget before the tokens up to it are stable. Four is
# enough on every input tested; this leaves an order of magnitude of margin.
_BOUNDARY_SLACK = 32


def _enc() -> tiktoken.Encoding:
    global _encoding
    if _encoding is None:
        try:
            _encoding = tiktoken.get_encoding("o200k_base")
        except Exception:  # pragma: no cover - fallback for older tiktoken
            _encoding = tiktoken.get_encoding("cl100k_base")
    return _encoding


def _count_tokens_sync(text: str) -> int:
    return len(_enc().encode(text))


def _trim_to_tokens_sync(text: str, max_tokens: int) -> str:
    """Reference trim: encode everything, keep the first ``max_tokens`` tokens."""
    if max_tokens <= 0:
        return ""
    enc = _enc()
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return enc.decode(tokens[:max_tokens])


def _trim_sync(text: str, max_tokens: int) -> str:
    """``_trim_to_tokens_sync``, but skipping the encode of text it would discard.

    Encodes a character slice rather than the whole input, then checks the
    result: a slice that did not clear ``max_tokens`` (plus a boundary margin)
    may have cut off text that was still within budget, so it is grown and
    retried. Only an overflowing slice proves the budget was reached, and its
    first ``max_tokens`` tokens are then the ones the full encode would have
    produced -- so this returns exactly what ``_trim_to_tokens_sync`` would,
    having encoded a fraction of the text.
    """
    if max_tokens <= 0:
        return ""
    enc = _enc()
    limit = max_tokens * _CHARS_PER_TOKEN_GUESS
    while limit < len(text):
        tokens = enc.encode(text[:limit])
        if len(tokens) > max_tokens + _BOUNDARY_SLACK:
            return enc.decode(tokens[:max_tokens])
        limit *= _SLICE_GROWTH
    return _trim_to_tokens_sync(text, max_tokens)


async def count_tokens(text: str) -> int:
    return await asyncio.to_thread(_count_tokens_sync, text)


async def trim_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    return await asyncio.to_thread(_trim_sync, text, max_tokens)
