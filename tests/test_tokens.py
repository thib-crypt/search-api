"""Tests for app.core.llm.tokens: async wrappers must not change behavior.

Compares the async, pre-truncated implementation against a plain synchronous
reference (encode -> slice -> decode, no character pre-truncation) for both
short and long inputs, to make sure the to_thread + char-pre-truncation path
is behavior-preserving.
"""

from __future__ import annotations

import pytest

from app.core.llm.tokens import _enc, count_tokens, trim_to_tokens


def _reference_trim(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    enc = _enc()
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return enc.decode(tokens[:max_tokens])


def _reference_count(text: str) -> int:
    return len(_enc().encode(text))


@pytest.mark.parametrize(
    "text,max_tokens",
    [
        ("", 10),
        ("short text", 100),
        ("short text", 0),
        ("word " * 5000, 50),  # long input, triggers char pre-truncation
        ("word " * 5000, 5000),  # long input, budget exceeds actual token count
        ("Some unicode: héllo wörld 日本語のテキスト " * 500, 30),
        # --- Low token density: BPE merges long runs of one character into
        # single tokens, so these sit at 16-128 chars/token rather than ~4. The
        # slice the trimmer starts from is far too small for them, and it has to
        # notice and grow it. Crawled markdown is full of exactly this: `---`
        # rules, deep indentation, runs of blank lines.
        (" " * 60000, 1000),
        ("=" * 60000, 1000),
        ("-" * 60000, 1000),
        ("\n" * 60000, 500),
        (("    " * 4 + "\n") * 5000, 1000),
        (("| cell " * 20 + "|\n" + "|---" * 20 + "|\n") * 500, 800),
        # Uniform input at a tiny budget: the boundary margin, not the growth
        # loop, is what makes these agree with the reference.
        ("." * 60000, 1),
        ("a" * 60000, 1),
    ],
)
async def test_trim_to_tokens_matches_reference(text: str, max_tokens: int) -> None:
    got = await trim_to_tokens(text, max_tokens)
    expected = _reference_trim(text, max_tokens)
    assert got == expected


@pytest.mark.parametrize(
    "text",
    ["", "short text", "word " * 5000],
)
async def test_count_tokens_matches_reference(text: str) -> None:
    assert await count_tokens(text) == _reference_count(text)
