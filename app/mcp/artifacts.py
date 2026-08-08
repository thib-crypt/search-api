"""Bounded, in-memory store for the full payload behind a truncated tool result (§3).

A tool result is charged to the agent's context window on every following turn,
so `app.mcp.budget` truncates it. The part that was cut still has to be
*reachable* — otherwise truncation is data loss — so the complete payload is
parked here under an id and surfaced as a `search-api://artifact/{id}` resource
link. The agent fetches the rest only if it turns out to need it.

In memory, not SQLite, and deliberately so: the §6.1 rule of the improvement plan
puts on disk what is expensive to *recompute*. An artefact is expensive to
*transport*, not to recompute — a crawl that produced it is minutes old and the
page is still there. Nothing here is worth surviving a restart, and a durable
store would only add a write on the hot path.

Bounded on both axes, like every other cache in this repo: `ttl` seconds and
`max_items` entries, least-recently-used evicted first.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class ArtifactPage:
    """One addressable slice of an artefact — typically one crawled URL."""

    title: str
    text: str
    # Free-form, small: url, status_code, word_count... Rendered next to the text
    # so an agent reading page 7 still knows which URL it is looking at.
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Artifact:
    id: str
    tool: str
    pages: list[ArtifactPage]
    created_at: float
    # Everything the tool computed, as JSON. What `search-api://artifact/{id}`
    # serves when the artefact is not paginated text.
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def total_chars(self) -> int:
        return sum(len(page.text) for page in self.pages)


class ArtifactStore:
    """TTL + LRU store of tool payloads, keyed by a short opaque id.

    Same shape as `app.core.cache.TTLCache` but not built on it: entries are
    looked up by an id this class *mints* rather than by a caller-supplied key,
    and eviction has to be observable (`stored` feeds a metric). Sharing the
    generic cache would have meant a key round-trip and a second bookkeeping
    layer for no gain.
    """

    def __init__(self, *, ttl: float = 1800.0, max_items: int = 256) -> None:
        self._ttl = ttl
        self._max_items = max(max_items, 1)
        self._store: OrderedDict[str, Artifact] = OrderedDict()
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._store)

    @staticmethod
    def mint_id() -> str:
        """An id, before there is anything to store under it.

        `app.mcp.budget` needs the id while it is still writing the text, because
        the truncation markers name it — and it only knows whether an artefact is
        warranted once that text is written.
        """
        return uuid4().hex[:12]

    async def put(
        self,
        tool: str,
        pages: list[ArtifactPage],
        payload: dict[str, Any] | None = None,
        artifact_id: str | None = None,
    ) -> Artifact:
        artifact = Artifact(
            id=artifact_id or self.mint_id(),
            tool=tool,
            pages=pages,
            created_at=time.monotonic(),
            payload=payload or {},
        )
        async with self._lock:
            self._prune()
            self._store[artifact.id] = artifact
            while len(self._store) > self._max_items:
                self._store.popitem(last=False)
        return artifact

    async def get(self, artifact_id: str) -> Artifact | None:
        async with self._lock:
            artifact = self._store.get(artifact_id)
            if artifact is None:
                return None
            if self._expired(artifact):
                del self._store[artifact_id]
                return None
            self._store.move_to_end(artifact_id)
            return artifact

    def _expired(self, artifact: Artifact) -> bool:
        return self._ttl > 0 and time.monotonic() - artifact.created_at > self._ttl

    def _prune(self) -> None:
        for key in [k for k, v in self._store.items() if self._expired(v)]:
            del self._store[key]

    def clear(self) -> None:
        self._store.clear()
