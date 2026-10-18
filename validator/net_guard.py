"""Reusable SSRF guard for the Readori source validator.

Pure functions only (standard library, no I/O besides optional DNS lookups).
The server uses it to refuse sources that point at private infrastructure; the
validation engine is expected to call it before every outbound request.

Typical use::

    ok, reason = check_url_allowed(url, allow_private=allow_private_from_env())
    if not ok:
        raise BlockedAddress(reason)

``allow_private=True`` (or ``READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK=1``) turns
the guard off for local development against 127.0.0.1 fixtures.

Limits worth knowing about: a check followed by a separate connect is still a
time-of-check/time-of-use race (DNS rebinding).  Callers that need to be strict
should pass the resolved address they validated to the HTTP client, and must
re-run the check on every redirect hop.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

ALLOW_PRIVATE_ENV = "READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK"
DEFAULT_SCHEMES = ("http", "https")
_TRUTHY = {"1", "true", "yes", "on"}

# Host names that never belong to the public internet, whatever DNS says.
_BLOCKED_HOST_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_BLOCKED_HOST_SUFFIXES = (
    ".localhost", ".local", ".localdomain", ".internal", ".intranet", ".lan", ".home.arpa", ".corp",
)

_NAT64 = ipaddress.ip_network("64:ff9b::/96")  # NAT64 well-known prefix

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], Iterable[str]]


def allow_private_from_env(environ: Mapping[str, str] | None = None) -> bool:
    """True when the operator explicitly allowed private-network targets."""

    source = os.environ if environ is None else environ
    return str(source.get(ALLOW_PRIVATE_ENV, "")).strip().lower() in _TRUTHY


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses hidden inside IPv6 forms (mapped, NAT64, 6to4, Teredo)."""

    found: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip in _NAT64:
        found.append(ipaddress.IPv4Address(ip.packed[-4:]))
    if ip.sixtofour is not None:  # 2002::/16
        found.append(ip.sixtofour)
    if ip.teredo is not None:  # 2001::/32 -> (server, client)
        found.extend(ip.teredo)
    # Deprecated IPv4-compatible form ::a.b.c.d (not mapped, but still routed by some stacks).
    if int(ip) >> 32 == 0 and int(ip) > 1:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return found


def is_public_ip(ip: IPAddress | str) -> bool:
    """True only for globally routable unicast addresses."""

    try:
        address = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # IPv4 hidden inside an IPv6 form decides the verdict; older Pythons mark the whole
        # ::ffff:0:0/96 range private even for public IPv4 targets.
        if address.ipv4_mapped is not None:
            return is_public_ip(address.ipv4_mapped)
        if address in _NAT64:
            return is_public_ip(ipaddress.IPv4Address(address.packed[-4:]))
        for embedded in _embedded_ipv4(address):
            if not is_public_ip(embedded):
                return False
    if (
        address.is_loopback or address.is_private or address.is_link_local or address.is_multicast
        or address.is_reserved or address.is_unspecified
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.is_site_local:
        return False
    return bool(address.is_global)


def _strip_host(host: str) -> str:
    text = (host or "").strip().strip("[]").rstrip(".").lower()
    return text.split("%", 1)[0] if ":" in text else text


def is_blocked_host_name(host: str) -> bool:
    """Static name check; catches ``localhost`` style names without DNS."""

    name = _strip_host(host)
    if not name:
        return True
    if name in _BLOCKED_HOST_NAMES:
        return True
    return any(name.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES)


def default_resolver(host: str) -> list[str]:
    """Resolve to every A/AAAA address; raises ``OSError`` when resolution fails."""

    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def _parse_legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """inet_aton forms that resolvers accept: ``2130706433``, ``0x7f.1``, ``0177.0.0.1``, ``127.1``."""

    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or any(not part for part in parts):
        return None
    numbers: list[int] = []
    for part in parts:
        try:
            if part.startswith(("0x", "0X")):
                numbers.append(int(part[2:] or "0", 16))
            elif len(part) > 1 and part.startswith("0"):
                numbers.append(int(part, 8))
            else:
                numbers.append(int(part, 10))
        except ValueError:
            return None
    *head, last = numbers
    if any(not 0 <= value <= 255 for value in head) or not 0 <= last < 256 ** (4 - len(head)):
        return None
    value = last
    for index, octet in enumerate(head):
        value |= octet << (8 * (3 - index))
    return ipaddress.IPv4Address(value)


def _parse_ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return _parse_legacy_ipv4(host)


def check_host_allowed(
    host: str,
    allow_private: bool = False,
    *,
    resolver: Resolver | None = None,
    fail_closed_on_dns_error: bool = True,
) -> tuple[bool, str]:
    """Return ``(allowed, reason)``; ``reason`` is empty when allowed.

    Every resolved address must be public.  Reasons are stable codes the caller
    can switch on: ``empty-host``, ``blocked-name``, ``blocked-address:<ip>``,
    ``unresolvable``.
    """

    if allow_private:
        return True, ""
    name = _strip_host(host)
    if not name:
        return False, "empty-host"
    literal = _parse_ip_literal(name)
    if literal is not None:
        return (True, "") if is_public_ip(literal) else (False, f"blocked-address:{literal}")
    if is_blocked_host_name(name):
        return False, "blocked-name"
    try:
        addresses = list((resolver or default_resolver)(name))
    except (OSError, UnicodeError):
        addresses = []
    if not addresses:
        return (False, "unresolvable") if fail_closed_on_dns_error else (True, "")
    for address in addresses:
        if not is_public_ip(address):
            return False, f"blocked-address:{address}"
    return True, ""


def is_public_address(host: str, *, resolver: Resolver | None = None) -> bool:
    """True when ``host`` (name or IP literal) only resolves to public addresses.

    Unresolvable hosts are treated as not public (fail closed).
    """

    return check_host_allowed(host, False, resolver=resolver)[0]


def check_url_allowed(
    url: str,
    allow_private: bool = False,
    *,
    resolver: Resolver | None = None,
    schemes: Iterable[str] = DEFAULT_SCHEMES,
    fail_closed_on_dns_error: bool = True,
) -> tuple[bool, str]:
    """Validate an outbound URL; returns ``(allowed, reason)``.

    Rejects non-http(s) schemes, missing hosts and any host that resolves to a non-public address.  With
    ``allow_private=True`` only the scheme and host presence are enforced.
    """

    try:
        parts = urlsplit((url or "").strip())
        scheme = (parts.scheme or "").lower()
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        return False, "invalid-url"
    if scheme not in {item.lower() for item in schemes}:
        return False, f"blocked-scheme:{scheme or 'none'}"
    if not host:
        return False, "empty-host"
    return check_host_allowed(
        host,
        allow_private,
        resolver=resolver,
        fail_closed_on_dns_error=fail_closed_on_dns_error,
    )
