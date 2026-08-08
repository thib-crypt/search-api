"""Construction and mounting of the MCP server (§0, §6.1, §6.4).

The server runs **in the API process**, on `MCP_PATH`, not as a sidecar that
would call the HTTP API back. That decision is the whole reason this layer is
cheap: one browser pool bounded by `CRAWL_MAX_CONCURRENCY`, one SearXNG cache,
one cost ledger enforcing one daily cap, and one `JobManager` — so a job started
from a tool is the same job `GET /v1/jobs/{id}` reports on, down to the id.

The price of that decision is the gap this module exists to close. Mounting an
ASGI app runs it through the app's *middleware* (CORS → request context → rate
limit → compression) but never through its **router dependencies** — and
`require_api_key` and `enforce_daily_cost_cap` are exactly that. Left alone,
`/mcp` would be an unauthenticated door onto the same provider bill. So:

* auth is re-applied here as an MCP `TokenVerifier` over the same keys and the
  same constant-time comparison (§6.1);
* the rate-limit weight is topped up per tool in `app.mcp.tools` (§6.2), because
  the limiter runs before routing and prices `/mcp` at 1 point whatever it does;
* the daily cost cap is checked explicitly by the three spending tools (§6.3).

The server object is built **per application startup**, not at import: a
`StreamableHTTPSessionManager` may only be run once, so a process that starts the
app twice (every test that opens a `TestClient`) needs a fresh one each time. The
route registered on the FastAPI app is therefore a thin ASGI shim that resolves
the live app through `app.state`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import Receive, Scope, Send

from app import __version__
from app.api.deps import matched_label
from app.config import ANONYMOUS_LABEL, Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.applications import Starlette

logger = logging.getLogger("unified_search_api.mcp")

# The scope every issued token carries; `AuthSettings.required_scopes` checks it.
SCOPE = "search-api"

# Placeholder issuer. This server is a resource server with a static key list, not
# an OAuth deployment: there is no authorization server to advertise. The value is
# never dereferenced — `AuthSettings` simply requires one — and no protected
# resource metadata route is published, because `resource_server_url` stays None.
_ISSUER_PLACEHOLDER = "https://api-keys.invalid"

INSTRUCTIONS = """\
Web search, crawling and deep research, exposed as tools.

Pick by cost, which every tool description states up front:
  free and instant   web_search, map_site, deep_research_estimate, job_status
  browser time       fetch_url, crawl_site
  LLM credits        answer, extract_structured
  minutes + dollars  deep_research

The usual shape of a task is web_search to find out which pages matter, then
fetch_url to read them. Reach for answer when one sourced paragraph settles the
question, and for deep_research only when a cited report is genuinely what is
wanted — it costs minutes and dollars per call.

Tool results are capped so they cannot swamp your context. Anything cut is
marked in place and stays readable as a search-api://artifact/... resource.
Read search-api://capabilities for this instance's actual limits."""


class ApiKeyVerifier(TokenVerifier):
    """Bearer-token check over the API keys already configured for the HTTP API.

    Reuses `matched_label` verbatim, so `/mcp` accepts exactly the keys `/v1/*`
    accepts, compares them the same way (every candidate in full, no early exit),
    and — the part that matters downstream — resolves the same **label**. The
    label is what `client_id` carries; the key itself is dropped the moment the
    label is known and never reaches a log line, a metric or a ledger row.

    With `AUTH_ENABLED=false` every caller authenticates as `anonymous`, which is
    what `get_api_key_label` returns on the HTTP side. Same instance-wide policy,
    one place to change it.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        settings = get_settings()
        if not settings.auth_enabled:  # pragma: no cover - not installed in that case
            return AccessToken(token="", client_id=ANONYMOUS_LABEL, scopes=[SCOPE])
        label = matched_label(token, settings.api_key_labels)
        if label is None:
            return None
        return AccessToken(token="", client_id=label, scopes=[SCOPE])


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """DNS-rebinding settings for the streamable-HTTP transport (§6.4).

    The SDK turns this protection on automatically when the server binds
    localhost, with an allow-list of localhost Hosts. In a container the Host is
    the service name or the public domain, so that default would refuse **every**
    MCP request — a failure mode that looks like a broken transport rather than a
    misconfiguration.

    So the rule is explicit: `MCP_ALLOWED_HOSTS` empty means the check is off
    (correct behind a reverse proxy, which pins the Host itself); set it and only
    those Hosts are served. Origins are derived from the same list so a browser
    client on an allowed host is not refused by the second check.
    """
    hosts = settings.mcp_allowed_host_list
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = [f"{scheme}://{host}" for host in hosts for scheme in ("http", "https")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_server(state: Any) -> MCPServer[None]:
    """Build the MCP server over the clients already living on ``app.state``.

    ``state`` is duck-typed rather than annotated as `starlette.datastructures.State`
    so `python -m app.mcp` can hand in an equivalent object it assembled itself
    for the stdio transport (§8) without importing FastAPI.
    """
    from app.mcp import resources, tools

    settings = get_settings()
    # Auth is wired only when the instance has it on, because the SDK's bearer
    # middleware refuses a request with no Authorization header before any
    # verifier is consulted — there is no "verifier that accepts everyone".
    # Leaving it out is what makes `AUTH_ENABLED=false` behave the same on /mcp
    # as on /v1/*: open, and loudly warned about at startup by `app.main`.
    auth: AuthSettings | None = None
    verifier: ApiKeyVerifier | None = None
    if settings.auth_enabled:
        verifier = ApiKeyVerifier()
        auth = AuthSettings(
            issuer_url=_ISSUER_PLACEHOLDER,  # type: ignore[arg-type]
            # None on purpose: publishing protected-resource metadata would point
            # clients at an OAuth server that does not exist.
            resource_server_url=None,
            required_scopes=[SCOPE],
        )

    server: MCPServer[None] = MCPServer(
        name="unified-search-api",
        title="Unified Search API",
        version=__version__,
        instructions=INSTRUCTIONS,
        token_verifier=verifier,
        auth=auth,
    )
    tools.register(server, state)
    resources.register(server, state)
    return server


def build_http_app(server: MCPServer[None], settings: Settings) -> Starlette:
    """The streamable-HTTP ASGI app for ``server``, served at `MCP_PATH`.

    The inner route is created at the *same* path it is reached at, so the app can
    be attached to the FastAPI router as an exact-path route. Mounting it instead
    would put a `Mount` in front, whose regex only matches `MCP_PATH + "/"` — every
    client configured with `https://host/mcp` would eat a 307 before each request.
    """
    return server.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        transport_security=transport_security(settings),
    )


class McpEndpoint:
    """ASGI shim standing in for the MCP app, which only exists once started.

    Registered on the router at import time so the path is stable, but resolving
    the real app through `app.state` on every request: the session manager is
    rebuilt on each startup, and a route holding a reference to the first one
    would serve a dead manager after the second.

    A class rather than a function on purpose. Starlette's `Route` inspects its
    endpoint and wraps anything that *is* a function in `request_response`,
    which would call it with a single `Request` argument. An instance is left
    alone and used as the raw ASGI app it is — which is what the streamable-HTTP
    transport needs, since it owns its own request and response streams.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app = scope.get("app")
        mcp_app = getattr(getattr(app, "state", None), "mcp_app", None)
        if mcp_app is None:
            body = b'{"detail":"MCP server is not enabled on this instance."}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("latin-1")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await mcp_app(scope, receive, send)


mcp_endpoint = McpEndpoint()
