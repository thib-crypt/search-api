"""Pre-crawl relevance filtering of SERP results.

SearXNG hands back a title, a snippet, a domain and sometimes a date for free.
Crawling one of those results costs a Chromium tab and ~2000 tokens of model
context — whether the page turns out to be useful or not. Scoring the cheap
metadata first lets the engine *widen* the SERP (more recall) while crawling
fewer pages (less cost).

Three implementations behind one protocol:

* ``NoopReranker``  — keeps the engine's original behaviour (SERP order).
* ``LexicalReranker`` — BM25 over titles+snippets, plus domain/freshness/length
  signals. No network, microseconds, no cost.
* ``LLMReranker`` — one structured call per SERP query on titles+snippets only.
  Understands synonyms and intent; **degrades to the lexical ranker on any
  failure**, which is the main reason the lexical one exists.

``CachedReranker`` memoizes a decision on (goal, candidate URLs, top_k): the
engine's fanout produces heavily overlapping SERPs and the same arbitration
should not be paid twice.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Protocol

from app.core import metrics
from app.core.cache import TTLCache
from app.core.llm.client import LLMClient
from app.core.llm.usage import UsageTracker
from app.core.research import prompts
from app.core.research.dedup import normalize_url
from app.core.research.schemas import RankedResults
from app.models.search import SearchResultItem

logger = logging.getLogger("unified_search_api.research.rerank")


class Reranker(Protocol):
    async def rank(
        self,
        goal: str,
        results: list[SearchResultItem],
        top_k: int,
        *,
        time_range: str | None = None,
        usage: UsageTracker | None = None,
    ) -> list[SearchResultItem]: ...


class NoopReranker:
    """Keeps the search engine's own ordering."""

    async def rank(
        self,
        goal: str,
        results: list[SearchResultItem],
        top_k: int,
        *,
        time_range: str | None = None,
        usage: UsageTracker | None = None,
    ) -> list[SearchResultItem]:
        return results[:top_k]


# -- Lexical ---------------------------------------------------------------

# Domains that mostly re-host or aggregate other people's content: usually a
# worse read than the source they point at.
_AGGREGATOR_DOMAINS = (
    "pinterest.",
    "quora.com",
    "reddit.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "slideshare.net",
    "scribd.com",
    "answers.com",
)
_AGGREGATOR_PENALTY = 0.5  # multiplicative
_SHORT_SNIPPET_CHARS = 40
_SHORT_SNIPPET_PENALTY = 0.5
_FRESHNESS_BONUS = 1.0

_TIME_RANGE_DAYS = {"day": 1, "week": 7, "month": 31, "year": 366}

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _is_aggregator(url: str) -> bool:
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    return any(marker in host for marker in _AGGREGATOR_DOMAINS)


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _freshness_bonus(item: SearchResultItem, time_range: str | None) -> float:
    """Reward recency, but only when the caller asked for a time window."""
    if not time_range:
        return 0.0
    window = _TIME_RANGE_DAYS.get(time_range)
    published = _parse_date(item.published_date)
    if window is None or published is None:
        return 0.0
    age_days = (datetime.now(UTC) - published).days
    if age_days < 0:
        return 0.0
    # Full bonus for a same-day result, decaying to zero at the window edge.
    return _FRESHNESS_BONUS * max(0.0, 1.0 - age_days / window)


class LexicalReranker:
    """BM25 over ``title + snippet``, corpus = the results of this call only.

    Rare words weigh more than common ones, so a result echoing the distinctive
    terms of the research goal outranks one that merely repeats "the" and "how".
    Blind to meaning by construction ("electric car" vs "battery vehicle" share
    no token), which is exactly what the LLM ranker is for.
    """

    async def rank(
        self,
        goal: str,
        results: list[SearchResultItem],
        top_k: int,
        *,
        time_range: str | None = None,
        usage: UsageTracker | None = None,
    ) -> list[SearchResultItem]:
        if not results:
            return []
        scored = self.score(goal, results, time_range=time_range)
        # Stable: equal scores keep the search engine's ordering.
        order = sorted(range(len(results)), key=lambda i: (-scored[i], i))
        return [results[i] for i in order[:top_k]]

    def score(
        self,
        goal: str,
        results: list[SearchResultItem],
        *,
        time_range: str | None = None,
    ) -> list[float]:
        docs = [_tokenize(f"{r.title} {r.snippet}") for r in results]
        lengths = [len(d) for d in docs]
        avg_len = (sum(lengths) / len(lengths)) or 1.0
        n_docs = len(docs)

        doc_freq: Counter[str] = Counter()
        for doc in docs:
            doc_freq.update(set(doc))

        query_terms = set(_tokenize(goal))
        scores: list[float] = []
        for doc, length, item in zip(docs, lengths, results, strict=True):
            counts = Counter(doc)
            score = 0.0
            for term in query_terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                df = doc_freq[term]
                idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
                norm = tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * length / avg_len))
                score += idf * norm
            if _is_aggregator(item.url):
                score *= _AGGREGATOR_PENALTY
            if len(item.snippet.strip()) < _SHORT_SNIPPET_CHARS:
                score -= _SHORT_SNIPPET_PENALTY
            score += _freshness_bonus(item, time_range)
            scores.append(score)
        return scores


# -- LLM -------------------------------------------------------------------


class LLMReranker:
    """One structured call per SERP query, on titles and snippets only.

    Cheap relative to what it saves: ~600 tokens of metadata in, up to seven
    uncrawled pages (~14 000 tokens) out. What it really costs is one extra
    round-trip of latency per SERP query, hence ``RESEARCH_RERANK_MODEL``.

    Never raises: a timeout, a provider error or an unusable answer silently
    falls back to the lexical ranker.
    """

    def __init__(self, llm: LLMClient, *, model: str | None = None) -> None:
        self._llm = llm
        self._model = model or None
        self._fallback = LexicalReranker()

    async def rank(
        self,
        goal: str,
        results: list[SearchResultItem],
        top_k: int,
        *,
        time_range: str | None = None,
        usage: UsageTracker | None = None,
    ) -> list[SearchResultItem]:
        if not results:
            return []
        try:
            ranked = await self._llm.structured(
                messages=[
                    {"role": "system", "content": prompts.system_prompt()},
                    {"role": "user", "content": prompts.rerank_prompt(goal, results, top_k)},
                ],
                response_model=RankedResults,
                model=self._model,
                # §6.5: the reranker's own call is real spend on the run's bill.
                usage=usage,
            )
            keep = self._valid_indices(ranked, len(results))
        except Exception as exc:  # noqa: BLE001 - degradation is the contract here
            logger.warning("LLM rerank failed, falling back to lexical: %s", exc)
            return await self._fallback.rank(goal, results, top_k, time_range=time_range)

        if not keep:
            logger.warning("LLM rerank returned no usable index, falling back to lexical")
            return await self._fallback.rank(goal, results, top_k, time_range=time_range)
        if ranked.dropped_reason:
            logger.debug("LLM rerank dropped %s", ranked.dropped_reason)
        return [results[i] for i in keep[:top_k]]

    @staticmethod
    def _valid_indices(ranked: RankedResults, count: int) -> list[int]:
        seen: set[int] = set()
        out: list[int] = []
        for index in ranked.keep:
            if 0 <= index < count and index not in seen:
                seen.add(index)
                out.append(index)
        return out


# -- Memoization -----------------------------------------------------------


class CachedReranker:
    """Memoizes an inner reranker's decision for a short window.

    Keyed by goal + candidate URL set + top_k, so two branches converging on the
    same SERP pay the arbitration once. Only URLs are stored, and the result is
    rebuilt from the live items, so a cache hit can never resurrect a stale
    title or snippet.
    """

    def __init__(self, inner: Reranker, *, ttl: float, max_size: int = 512) -> None:
        self._inner = inner
        self._cache: TTLCache[list[str]] = TTLCache(ttl=ttl, max_size=max_size)

    @staticmethod
    def _key(goal: str, results: list[SearchResultItem], top_k: int) -> str:
        urls = sorted(normalize_url(r.url) for r in results)
        digest = hashlib.sha256("\n".join([goal, str(top_k), *urls]).encode()).hexdigest()
        return digest

    async def rank(
        self,
        goal: str,
        results: list[SearchResultItem],
        top_k: int,
        *,
        time_range: str | None = None,
        usage: UsageTracker | None = None,
    ) -> list[SearchResultItem]:
        if not results:
            return []
        key = self._key(goal, results, top_k)
        by_url = {normalize_url(r.url): r for r in results}
        cached = await self._cache.get(key)
        if cached is not None:
            hit = [by_url[url] for url in cached if url in by_url]
            if hit:
                metrics.record_cache("rerank", hit=True)
                return hit
        metrics.record_cache("rerank", hit=False)
        ranked = await self._inner.rank(goal, results, top_k, time_range=time_range, usage=usage)
        await self._cache.set(key, [normalize_url(r.url) for r in ranked])
        return ranked


def build_reranker(
    mode: str,
    *,
    llm: LLMClient | None = None,
    model: str = "",
    cache_ttl: float = 600.0,
) -> Reranker:
    """Pick an implementation from config. Called once, at application startup."""
    if mode == "off":
        return NoopReranker()
    if mode == "lexical":
        return LexicalReranker()
    if mode == "llm":
        if llm is None:  # pragma: no cover - defensive, the app always has a client
            logger.warning("RESEARCH_RERANK=llm without an LLM client; using lexical")
            return LexicalReranker()
        reranker: Reranker = LLMReranker(llm, model=model)
        # Lexical/noop are free, caching them would only add bookkeeping.
        return CachedReranker(reranker, ttl=cache_ttl) if cache_ttl > 0 else reranker
    logger.warning("Unknown RESEARCH_RERANK=%r; using lexical", mode)
    return LexicalReranker()
