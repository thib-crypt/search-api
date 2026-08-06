"""Shared SQLite plumbing for the two things that must survive a restart (§6.1).

A finished research report cost minutes and cents to produce, and a daily spend
cap that resets whenever the container restarts protects nothing. Both live in
one file on one volume — no extra container, which is the whole point of §0.

Each store opens its own connection to that file. With WAL enabled that is fine
(and simpler than passing one connection around): a single-worker process has no
writer contention to speak of, and readers never block the writer.
"""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger("unified_search_api.db")


def _prepare_dir(db_path: str) -> Path:
    """Create the database's parent directory. Sync on purpose: one `mkdir` at
    startup is not worth a thread hop, and doing it here keeps the `async`
    connect free of blocking-filesystem lint noise."""
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def connect(db_path: str) -> aiosqlite.Connection:
    """Open ``db_path``, creating its directory if needed.

    Creating the directory here rather than in the Dockerfile keeps a bind-mounted
    volume working whatever its initial state.
    """
    path = _prepare_dir(db_path)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    # WAL: readers never block the writer, and a crash mid-write cannot corrupt
    # the file. NORMAL sync: a power cut can lose the last transaction, which for
    # progress events and cost counters is a far better trade than fsync per row.
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.commit()
    return conn
