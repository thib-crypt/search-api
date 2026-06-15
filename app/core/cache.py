"""Tiny async-safe TTL + LRU cache used to memoize expensive backend calls.

Kept deliberately small and in-process: an ``OrderedDict`` behind an
``asyncio.Lock`` with per-entry expiry and bounded size (least-recently-used
entries evicted first). It can be swapped for Redis later behind the same
``get``/``set`` surface if cross-process sharing becomes necessary.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    def __init__(self, *, ttl: float, max_size: int) -> None:
        self._ttl = ttl
        self._max_size = max(max_size, 1)
        # key -> (expiry_monotonic, value); ordered by recency of use.
        self._store: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> V | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= time.monotonic():
                del self._store[key]
                return None
            self._store.move_to_end(key)  # mark as most-recently used
            return value

    async def set(self, key: str, value: V) -> None:
        async with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, value)
            self._store.move_to_end(key)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)  # evict least-recently used

    def clear(self) -> None:
        self._store.clear()
