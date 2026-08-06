"""Test-wide environment.

Auth and rate limiting are on by default in production (§2.13), so without this
every test would need a key. The suite runs as a development instance with both
off; the tests that care about auth turn it back on themselves via monkeypatch +
``get_settings.cache_clear()``.

Set before `app.main` is imported: `Settings` is read at import time.
"""

from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "development")
os.environ["AUTH_ENABLED"] = "false"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["API_KEYS"] = ""
# Deterministic crawling in tests: no politeness sleep, and no DNS lookups for
# the fake hosts the fixtures use. The SSRF tests opt back into the real guard.
os.environ["CRAWL_PER_HOST_DELAY"] = "0"
os.environ["CRAWL_ALLOW_PRIVATE_NETWORKS"] = "true"

import pytest  # noqa: E402

from app.api.middleware import reset_rate_limiter  # noqa: E402
from app.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_global_state():
    """Keep per-process state (settings cache, limiter windows) from leaking."""
    get_settings.cache_clear()
    reset_rate_limiter()
    yield
    get_settings.cache_clear()
    reset_rate_limiter()
