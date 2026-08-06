"""SSRF guard for every URL the server is asked to fetch.

The crawl endpoints take arbitrary URLs from untrusted clients, so without a
check the API is a proxy into its own network: cloud metadata endpoints
(``169.254.169.254``), the SearXNG container, the API's own admin surface, or
anything else reachable from the container.

The guard is deliberately conservative:

* only ``http`` / ``https``, only an allow-listed set of ports;
* the hostname is resolved **before** the browser is handed the URL, and every
  resolved address must be globally routable — loopback, private, link-local,
  CGNAT, multicast and reserved ranges are all refused (``is_global`` covers
  them for both IPv4 and IPv6);
* optional domain allow/deny lists on top;
* :meth:`UrlGuard.check` is re-run on the URL a crawl actually landed on, so a
  redirect (or DNS rebinding between resolution and connection) into a private
  address is caught before the content is returned.

DNS resolution goes through the event loop's resolver in a thread, so a slow
resolver cannot block the single worker.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import Settings

_DEFAULT_PORTS = {"http": 80, "https": 443}


class BlockedUrlError(ValueError):
    """Raised when a URL fails the network guard. The message is client-safe."""


@dataclass(frozen=True)
class UrlGuard:
    allow_private: bool = False
    allowed_ports: frozenset[int] = frozenset({80, 443, 8080})
    allowlist: frozenset[str] = frozenset()
    denylist: frozenset[str] = frozenset()

    @classmethod
    def from_settings(cls, settings: Settings) -> UrlGuard:
        return cls(
            allow_private=settings.crawl_allow_private_networks,
            allowed_ports=settings.crawl_allowed_port_set,
            allowlist=settings.crawl_domain_allowlist_set,
            denylist=settings.crawl_domain_denylist_set,
        )

    # -- Checks -------------------------------------------------------------

    def check_syntax(self, url: str) -> tuple[str, int]:
        """Validate scheme, host, port and domain lists. Returns ``(host, port)``.

        No DNS lookup: cheap enough to run on every discovered link.
        """
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            raise BlockedUrlError(f"scheme '{parts.scheme or ''}' is not allowed (http/https only)")

        try:
            host = parts.hostname
            port = parts.port
        except ValueError as exc:  # malformed port
            raise BlockedUrlError(f"invalid URL: {exc}") from exc
        if not host:
            raise BlockedUrlError("URL has no host")

        host = host.lower().rstrip(".")
        port = port or _DEFAULT_PORTS[scheme]
        if self.allowed_ports and port not in self.allowed_ports:
            raise BlockedUrlError(f"port {port} is not allowed")

        if _matches(host, self.denylist):
            raise BlockedUrlError(f"host '{host}' is denylisted")
        if self.allowlist and not _matches(host, self.allowlist):
            raise BlockedUrlError(f"host '{host}' is not in the domain allowlist")

        return host, port

    async def check(self, url: str) -> None:
        """Full check: syntax, then every address the host resolves to."""
        host, port = self.check_syntax(url)
        if self.allow_private:
            return

        literal = _as_ip(host)
        addresses = [literal] if literal is not None else await _resolve(host, port)
        if not addresses:
            raise BlockedUrlError(f"host '{host}' does not resolve")
        for ip in addresses:
            if not ip.is_global:
                raise BlockedUrlError(
                    f"host '{host}' resolves to non-public address {ip} "
                    "(private, loopback or link-local)"
                )

    async def partition(self, urls: list[str]) -> tuple[list[str], dict[str, str]]:
        """Split ``urls`` into (allowed, {rejected_url: reason}).

        Resolution runs concurrently: a batch of 50 URLs costs one DNS round-trip,
        not fifty in series.
        """
        checks = await asyncio.gather(
            *(self.check(u) for u in urls), return_exceptions=True
        )
        allowed: list[str] = []
        rejected: dict[str, str] = {}
        for url, outcome in zip(urls, checks, strict=True):
            if outcome is None:
                allowed.append(url)
            elif isinstance(outcome, BlockedUrlError):
                rejected[url] = str(outcome)
            elif isinstance(outcome, BaseException):
                rejected[url] = f"could not verify URL: {outcome}"
        return allowed, rejected


def _matches(host: str, domains: frozenset[str]) -> bool:
    """True when ``host`` equals one of ``domains`` or is a subdomain of one."""
    return any(host == d or host.endswith(f".{d}") for d in domains)


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a bare IP literal, including the ``[::1]`` bracket form already stripped."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


async def _resolve(
    host: str, port: int
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedUrlError(f"host '{host}' does not resolve ({exc.strerror or exc})") from exc
    except OSError as exc:  # pragma: no cover - resolver failure
        raise BlockedUrlError(f"could not resolve host '{host}': {exc}") from exc

    addresses = []
    for info in infos:
        sockaddr = info[4]
        ip = _as_ip(str(sockaddr[0]).split("%", 1)[0])  # strip IPv6 zone id
        if ip is not None:
            addresses.append(ip)
    return addresses
