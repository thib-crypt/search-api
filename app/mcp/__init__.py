"""MCP server layer (Phase 14).

Everything protocol-shaped lives under this package and nothing outside it knows
MCP exists — no file in `app/services/`, `app/core/` or `app/models/` was touched
to make this work, which was the test of the in-process decision (§0). If a
service had needed changing for MCP, the abstraction would have been in the
wrong place.

The one exception is deliberate and small: `app/api/middleware.py` grew a
`charge()` helper, because the weighted rate-limit window is genuinely shared
between the two surfaces and duplicating it would have meant two budgets.

See `MCP_REFERENCE.md` for the tool catalogue and client configuration.
"""

from __future__ import annotations

from app.mcp.server import build_http_app, build_server, mcp_endpoint

__all__ = ["build_http_app", "build_server", "mcp_endpoint"]
