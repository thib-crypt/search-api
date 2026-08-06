"""Network guard (§2.1): the crawler must not be usable as a proxy into the LAN.

These tests never touch the real resolver: addresses are IP literals (checked
without DNS) or the resolver is stubbed.
"""

from __future__ import annotations

import ipaddress

import pytest
from starlette.testclient import TestClient

from app.config import get_settings
from app.core.net import guard as guard_mod
from app.core.net.guard import BlockedUrlError, UrlGuard
from app.main import app

# The canonical SSRF target: AWS/GCP/Azure instance credentials.
METADATA_URL = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"

BLOCKED_ADDRESSES = [
    METADATA_URL,
    "http://127.0.0.1/admin",
    "http://localhost:8080/config",  # resolves to loopback
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://100.64.0.1/",  # CGNAT
    "http://0.0.0.0/",
    "http://[::1]/",
    "http://[fd00::1]/",
]


@pytest.fixture
def strict() -> UrlGuard:
    return UrlGuard()


@pytest.mark.parametrize("url", BLOCKED_ADDRESSES)
async def test_private_and_loopback_addresses_are_refused(strict: UrlGuard, url: str) -> None:
    with pytest.raises(BlockedUrlError):
        await strict.check(url)


async def test_public_address_passes(strict: UrlGuard) -> None:
    await strict.check("https://8.8.8.8/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://a/", "//x"])
async def test_non_http_schemes_are_refused(strict: UrlGuard, url: str) -> None:
    with pytest.raises(BlockedUrlError, match="scheme"):
        await strict.check(url)


async def test_non_standard_ports_are_refused(strict: UrlGuard) -> None:
    with pytest.raises(BlockedUrlError, match="port 22"):
        await strict.check("http://8.8.8.8:22/")
    await strict.check("http://8.8.8.8:8080/")  # in the default allow-list


async def test_denylist_blocks_host_and_subdomains() -> None:
    guard = UrlGuard(allow_private=True, denylist=frozenset({"searxng", "internal.corp"}))
    with pytest.raises(BlockedUrlError, match="denylisted"):
        await guard.check("http://searxng:8080/config")
    with pytest.raises(BlockedUrlError, match="denylisted"):
        await guard.check("https://api.internal.corp/")
    await guard.check("https://public.example/")


async def test_allowlist_restricts_to_listed_domains() -> None:
    guard = UrlGuard(allow_private=True, allowlist=frozenset({"example.com"}))
    await guard.check("https://example.com/page")
    await guard.check("https://docs.example.com/page")
    with pytest.raises(BlockedUrlError, match="allowlist"):
        await guard.check("https://notexample.com/")


async def test_allow_private_opt_out() -> None:
    await UrlGuard(allow_private=True).check("http://127.0.0.1:8080/")


async def test_hostname_resolving_to_private_ip_is_refused(monkeypatch) -> None:
    """DNS is what makes this exploitable: `evil.com` can point at 127.0.0.1."""

    async def fake_resolve(host: str, port: int):
        return [ipaddress.ip_address("127.0.0.1")]

    monkeypatch.setattr(guard_mod, "_resolve", fake_resolve)
    with pytest.raises(BlockedUrlError, match="non-public"):
        await UrlGuard().check("https://rebind.example/")


async def test_hostname_resolving_to_public_ip_is_allowed(monkeypatch) -> None:
    async def fake_resolve(host: str, port: int):
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(guard_mod, "_resolve", fake_resolve)
    await UrlGuard().check("https://example.com/")


async def test_a_single_private_answer_blocks_the_whole_host(monkeypatch) -> None:
    """One public and one private address must not be treated as 'mostly fine'."""

    async def fake_resolve(host: str, port: int):
        return [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("10.0.0.5")]

    monkeypatch.setattr(guard_mod, "_resolve", fake_resolve)
    with pytest.raises(BlockedUrlError, match="non-public"):
        await UrlGuard().check("https://split-horizon.example/")


async def test_unresolvable_host_is_refused(monkeypatch) -> None:
    """A DNS failure must fail closed, not fall through to the browser."""
    import socket

    def boom(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(BlockedUrlError, match="does not resolve"):
        await UrlGuard().check("https://nope.invalid/")


async def test_host_with_no_addresses_is_refused(monkeypatch) -> None:
    async def empty(host: str, port: int):
        return []

    monkeypatch.setattr(guard_mod, "_resolve", empty)
    with pytest.raises(BlockedUrlError, match="does not resolve"):
        await UrlGuard().check("https://ghost.example/")


async def test_partition_splits_allowed_from_rejected(strict: UrlGuard) -> None:
    allowed, rejected = await strict.partition(
        ["https://8.8.8.8/ok", METADATA_URL, "https://1.1.1.1/also-ok"]
    )
    assert allowed == ["https://8.8.8.8/ok", "https://1.1.1.1/also-ok"]
    assert list(rejected) == [METADATA_URL]


# -- Through the HTTP surface ------------------------------------------------


@pytest.fixture
def strict_app(monkeypatch):
    """A client whose guard is the production one (private networks refused)."""
    monkeypatch.setenv("CRAWL_ALLOW_PRIVATE_NETWORKS", "false")
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def test_crawl_endpoint_refuses_the_metadata_endpoint(strict_app) -> None:
    resp = strict_app.post("/v1/crawl", json={"urls": [METADATA_URL]})
    assert resp.status_code == 400
    assert "169.254.169.254" in resp.json()["detail"]


def test_crawl_endpoint_refuses_internal_services(strict_app) -> None:
    resp = strict_app.post(
        "/v1/crawl", json={"urls": ["http://searxng:8080/config", "http://10.0.0.1/admin"]}
    )
    assert resp.status_code == 400


def test_site_crawl_endpoint_refuses_loopback(strict_app) -> None:
    resp = strict_app.post("/v1/crawl/site", json={"url": "http://127.0.0.1:8080/"})
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"]
