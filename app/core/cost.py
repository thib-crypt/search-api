"""Per-key daily spend ledger — the durable half of §6.4b.

`UsageTracker` measures one request. This measures a *key*, across every request
it makes in a UTC day, so `DAILY_COST_CAP_USD` can turn a runaway client away at
the door instead of after it has spent the month's budget.

Two implementations, chosen by `JOB_BACKEND`:

* `SqliteCostLedger` — a `cost_ledger(label, day, cost_usd)` table in the same
  file as the jobs (§6.1). A cap that resets on every restart protects nothing,
  which is exactly why the plan puts these counters in SQLite.
* `InMemoryCostLedger` — **known limitation**: the running totals are lost on
  restart, so a restart hands every key a fresh daily budget. Consistent with
  what `JOB_BACKEND=memory` means everywhere else (§0: memory mode has no
  persistence at all); use the sqlite backend if the cap has to hold.

Attribution is always by **label**, never by key (§6.2).

Like every other meter in this phase, a ledger failure degrades: `add` logs and
moves on, `spent_today` reports 0 rather than raising. A broken counter must not
be able to lock every caller out of the API.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger("unified_search_api.cost")


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


class CostLedger(Protocol):
    async def start(self) -> None: ...

    async def add(self, label: str, cost_usd: float) -> None:
        """Add ``cost_usd`` to ``label``'s total for today."""

    async def spent_today(self, label: str) -> float:
        """What ``label`` has spent so far today, in USD. 0 when unknown."""


class InMemoryCostLedger:
    """Process-local totals. Reset on restart — see the module docstring."""

    def __init__(self) -> None:
        self._totals: dict[tuple[str, str], float] = {}

    async def start(self) -> None:
        return None

    async def add(self, label: str, cost_usd: float) -> None:
        if cost_usd <= 0:
            return
        key = (label, utc_day())
        self._totals[key] = self._totals.get(key, 0.0) + cost_usd
        # Yesterday's rows are dead weight; one day of labels is all that matters.
        today = utc_day()
        if len(self._totals) > 1:
            for stale in [k for k in self._totals if k[1] != today]:
                del self._totals[stale]

    async def spent_today(self, label: str) -> float:
        return self._totals.get((label, utc_day()), 0.0)


class SqliteCostLedger:
    """Durable totals, in the job database's `cost_ledger` table."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: Any = None

    async def start(self) -> None:
        from app.core.db import connect

        try:
            self._conn = await connect(self.db_path)
            await self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cost_ledger ("
                "label TEXT NOT NULL, day TEXT NOT NULL, "
                "cost_usd REAL NOT NULL DEFAULT 0, PRIMARY KEY (label, day))"
            )
            await self._conn.commit()
        except Exception:  # noqa: BLE001 - degrade to "no accounting", not "no API"
            logger.exception("Could not open the cost ledger; daily caps will not be enforced")
            self._conn = None

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.exception("Closing the cost ledger failed")
            self._conn = None

    async def add(self, label: str, cost_usd: float) -> None:
        if self._conn is None or cost_usd <= 0:
            return
        try:
            await self._conn.execute(
                "INSERT INTO cost_ledger (label, day, cost_usd) VALUES (?, ?, ?) "
                "ON CONFLICT(label, day) DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd",
                (label, utc_day(), float(cost_usd)),
            )
            await self._conn.commit()
        except Exception:  # noqa: BLE001 - a lost cent must not fail a served request
            logger.exception("Could not record spend for label %r", label)

    async def spent_today(self, label: str) -> float:
        if self._conn is None:
            return 0.0
        try:
            async with self._conn.execute(
                "SELECT cost_usd FROM cost_ledger WHERE label = ? AND day = ?",
                (label, utc_day()),
            ) as cursor:
                row = await cursor.fetchone()
            return float(row[0]) if row else 0.0
        except Exception:  # noqa: BLE001 - unknown spend is not "over budget"
            logger.exception("Could not read spend for label %r", label)
            return 0.0


def build_cost_ledger(backend: str, db_path: str) -> CostLedger:
    """Pick an implementation from config. Called once, at application startup."""
    return SqliteCostLedger(db_path) if backend == "sqlite" else InMemoryCostLedger()
