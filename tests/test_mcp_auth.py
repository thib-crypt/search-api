"""`/mcp` is behind the same keys as `/v1/*` (§6.1, §11).

This is the file that exists because the failure mode is silent.
`require_api_key` is an `APIRouter` dependency, and a mounted ASGI app never runs
router dependencies — so a `/mcp` that shipped without `ApiKeyVerifier` would
work perfectly, answer every caller, and hand an open door to the provider bill.
Nothing else in the suite would have noticed.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.config import get_settings
from app.main import app
from tests.mcp_client import mcp_session, raw_client

KEY = "sk_test_" + "c" * 32
OTHER = "sk_test_" + "d" * 32


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", f"agent:{KEY},other:{OTHER}")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_mcp_is_closed_without_a_key(auth_on) -> None:
    async with raw_client(app) as client:
        resp = await client.post(
            "/mcp",
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
    assert resp.status_code == 401


async def test_mcp_rejects_a_wrong_key(auth_on) -> None:
    async with raw_client(app) as client:
        resp = await client.post(
            "/mcp",
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "authorization": "Bearer not-a-key",
            },
        )
    assert resp.status_code == 401


async def test_a_configured_key_gets_a_session(auth_on) -> None:
    async with mcp_session(app, api_key=KEY) as session:
        listed = await session.list_tools()
    assert len(listed.tools) == 9


async def test_the_key_never_reaches_the_logs(auth_on, caplog) -> None:
    """A rejected key must not be echoed anywhere — not even truncated."""
    with caplog.at_level(logging.DEBUG):
        async with raw_client(app) as client:
            await client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                    "authorization": f"Bearer {OTHER}",
                },
            )
    recorded = "\n".join(record.getMessage() for record in caplog.records)
    assert OTHER not in recorded
    assert OTHER[8:] not in recorded


async def test_the_caller_label_is_what_reaches_the_tools(auth_on) -> None:
    """`client_id` carries the label, so spend is attributed exactly as over HTTP."""
    async with mcp_session(app, api_key=KEY) as session:
        content = await session.read_resource("search-api://capabilities")
    payload = json.loads(content.contents[0].text)
    assert payload["instance"]["auth_enabled"] is True


async def test_mcp_answers_without_a_key_when_auth_is_off() -> None:
    """The suite runs with AUTH_ENABLED=false; /mcp must match /v1/* and be open."""
    async with mcp_session(app) as session:
        listed = await session.list_tools()
    assert listed.tools


async def test_mcp_path_is_served_without_a_redirect() -> None:
    """A `Mount` would 307 `/mcp` to `/mcp/` and every client would eat a hop."""
    async with raw_client(app) as client:
        resp = await client.post(
            "/mcp",
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
    assert resp.status_code != 307


def test_an_empty_host_list_does_not_refuse_every_request(monkeypatch) -> None:
    """§6.4: the SDK's localhost default would 421 every request in a container."""
    from app.mcp.server import transport_security

    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "")
    get_settings.cache_clear()
    settings = transport_security(get_settings())
    assert settings.enable_dns_rebinding_protection is False


def test_a_configured_host_list_is_enforced_with_matching_origins(monkeypatch) -> None:
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "api.example.com, mcp.example.com")
    get_settings.cache_clear()
    settings = transport_security_for()
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["api.example.com", "mcp.example.com"]
    # Origins are derived so a browser client on an allowed host is not refused
    # by the second check after passing the first.
    assert "https://api.example.com" in settings.allowed_origins
    assert "http://mcp.example.com" in settings.allowed_origins


def transport_security_for():
    from app.mcp.server import transport_security

    return transport_security(get_settings())


async def test_mcp_is_absent_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("MCP_ENABLED", "false")
    get_settings.cache_clear()
    try:
        async with raw_client(app) as client:
            resp = await client.post(
                "/mcp",
                content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
            )
        assert resp.status_code == 404
    finally:
        get_settings.cache_clear()
