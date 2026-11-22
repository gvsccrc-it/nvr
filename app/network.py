"""Remote access (#132): who is really calling the NVR, over what, and whether that is safe.

The NVR is reached remotely through a tunnel or reverse proxy, never through a relay of ours. A proxy hides the caller behind its own address and
says who the caller was in headers (``X-Forwarded-For``, ``-Proto``, ``-Host``), which anybody who can reach the NVR directly could also send.
So forwarded headers are believed **only from a peer listed in ``NVR_TRUSTED_PROXIES``** (addresses and networks, nothing else):

* the caller's address is the first entry, reading ``X-Forwarded-For`` from the right, that is not itself a trusted proxy; a malformed entry, or
  more than ten, means the header is ignored and the peer's address is used;
* the scheme (HTTPS or not) and the host come from ``X-Forwarded-Proto`` and ``-Host`` the same way, and from the connection otherwise.

``NVR_TRUST_FORWARDED_FOR=true`` (before trusted proxies existed) still trusts the last ``X-Forwarded-For`` entry from any sender; it is only safe
when the NVR is reachable through the proxy alone, and Settings says so.

Whether a caller is *remote* or *local* is decided by ``classify``, the first of these that fits its address (#458):

1. inside ``NVR_REMOTE_NETWORKS``: remote. This wins over everything below; it is how a VPN or site-to-site link that uses private
   addresses is made to count as outside.
2. inside ``NVR_LOCAL_NETWORKS``: local. The administrator said so.
3. inside a tunnel's range: remote. A tunnel's range is one named in ``NVR_TUNNEL_NETWORKS`` (the WireGuard overlay names its own) or
   the network of a tunnel interface on this machine (``wg*``, ``tun*``, ``tap*``). People on a tunnel are not at the site, though
   their addresses are private ones; listing the range in ``NVR_LOCAL_NETWORKS`` (rule 2) is how an administrator says they are.
4. loopback, a private range or a link-local range: local. Tailscale's range is not among them.
5. anything else: remote. An address that cannot be read counts as remote.

Wherever the caller cannot be known it counts as remote: a trusted proxy that reports no usable address, forwarded headers from a peer that
is not a trusted proxy, and anything that rests on ``NVR_TRUST_FORWARDED_FOR``, which any sender could write.
"""
from __future__ import annotations

import ipaddress
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address

DEFAULT_LOCAL_NETWORKS = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fe80::/10", "fc00::/7")
MAX_HOPS = 10
_HOST = re.compile(r"^(?:[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*|\[[0-9a-f:.]+\])(?::\d{1,5})?$")


TUNNEL_INTERFACE = re.compile(r"^(wg|tun|tap)")  # WireGuard, and the interfaces OpenVPN and others make
_INTERFACES_FOR_SECONDS = 60.0
_interfaces_read: tuple[float, list[tuple[Network, str]]] | None = None


def interfaces() -> list[tuple[Network, str]]:
    """The IPv4 network and name of each interface this machine has an address on, loopback left out. Read at most once a minute (it is
    asked for on requests). Linux only, and best effort: an empty list when they cannot be read."""
    global _interfaces_read
    now = time.monotonic()
    if _interfaces_read is None or now - _interfaces_read[0] > _INTERFACES_FOR_SECONDS:
        found = []
        for entry in _read_interfaces():
            text, _, name = entry.partition(" (")
            found.append((ipaddress.ip_network(text), name.rstrip(")")))
        _interfaces_read = (now, found)
    return _interfaces_read[1]


def interface_networks() -> list[str]:
    """The IPv4 networks this machine has an address on, loopback left out ("192.0.2.0/24 (eth0)"), for saying where the interface can be
    reached from (#541)."""
    return _read_interfaces()


def _read_interfaces() -> list[str]:
    import fcntl
    import socket
    import struct

    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for _, name in socket.if_nameindex():
                request = struct.pack("256s", name.encode()[:15])
                try:
                    address = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x8915, request)[20:24])  # SIOCGIFADDR
                    mask = socket.inet_ntoa(fcntl.ioctl(probe.fileno(), 0x891B, request)[20:24])  # SIOCGIFNETMASK
                except OSError:
                    continue  # No IPv4 address on this interface
                interface = ipaddress.ip_interface(f"{address}/{mask}")
                if not interface.ip.is_loopback:
                    found.append(f"{interface.network} ({name})")
    except (OSError, ValueError):
        return []
    return sorted(set(found))


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def parse_networks(text: str) -> tuple[list[Network], list[str]]:
    """The networks in a comma-separated list, and the entries that are not addresses or networks."""
    found: list[Network] = []
    bad: list[str] = []
    for item in (part.strip() for part in text.split(",")):
        if not item:
            continue
        try:
            found.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            bad.append(item[:64])
    return found, bad


def trusted_proxies() -> list[Network]:
    return parse_networks(os.environ.get("NVR_TRUSTED_PROXIES", ""))[0]


def local_networks() -> list[Network]:
    return parse_networks(",".join(DEFAULT_LOCAL_NETWORKS))[0] + listed_local_networks()


def listed_local_networks() -> list[Network]:
    return parse_networks(os.environ.get("NVR_LOCAL_NETWORKS", ""))[0]


def remote_networks() -> list[Network]:
    """Ranges that count as remote whatever else is true of them (``NVR_REMOTE_NETWORKS``)."""
    return parse_networks(os.environ.get("NVR_REMOTE_NETWORKS", ""))[0]


def tunnel_networks() -> list[Network]:
    """Ranges people reach the NVR from through a tunnel: those named in ``NVR_TUNNEL_NETWORKS``, and the network of each tunnel
    interface on this machine."""
    named = parse_networks(os.environ.get("NVR_TUNNEL_NETWORKS", ""))[0]
    return named + [network for network, name in interfaces() if TUNNEL_INTERFACE.match(name)]


def host_networks() -> list[Network]:
    """The networks this machine is on directly, tunnels left out: where "at the site" physically is."""
    return [network for network, name in interfaces() if not TUNNEL_INTERFACE.match(name)]


# Why an address was counted as it was, in words for Settings
REASONS = {
    "unreadable": "its address could not be read, and what cannot be known counts as remote",
    "listed_remote": "its address is inside NVR_REMOTE_NETWORKS",
    "listed_local": "its address is inside NVR_LOCAL_NETWORKS",
    "tunnel": "its address is in a tunnel's range, and people on a tunnel count as remote unless that range is listed in NVR_LOCAL_NETWORKS",
    "private_range": "its address is in a private, loopback or link-local range",
    "public": "its address is not in any local range",
    "unknown_caller": "it came through a proxy that is not trusted or did not say who the caller is, and a caller that cannot be known counts as remote",
}


def classify(address: str) -> tuple[bool, str]:
    """Whether an address counts as remote, and the rule that decided it (a key of ``REASONS``). The order is the module docstring's."""
    parsed = _ip(address)
    if parsed is None:
        return True, "unreadable"
    if _within(parsed, remote_networks()):
        return True, "listed_remote"
    if _within(parsed, listed_local_networks()):
        return False, "listed_local"
    if _within(parsed, tunnel_networks()):
        return True, "tunnel"
    if _within(parsed, parse_networks(",".join(DEFAULT_LOCAL_NETWORKS))[0]):
        return False, "private_range"
    return True, "public"


def legacy_forwarded_trust() -> bool:
    return _truthy("NVR_TRUST_FORWARDED_FOR")


def _ip(value: str) -> Address | None:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    return address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address  # ::ffff:127.0.0.1 is 127.0.0.1


def _entry(value: str) -> Address | None:
    """An address as a proxy writes it into ``X-Forwarded-For``: bare, or with a port (``1.2.3.4:5678``, ``[::1]:80``)."""
    value = value.strip()
    if value.startswith("["):
        address, closed, rest = value[1:].partition("]")
        return _ip(address) if closed and (not rest or re.fullmatch(r":\d{1,5}", rest)) else None
    if value.count(":") == 1:
        address, _, port = value.partition(":")
        return _ip(address) if port.isdigit() and len(port) <= 5 else None
    return _ip(value)


def _within(address: Address, networks: list[Network]) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def peer_address(connection: HTTPConnection) -> str:
    return connection.client.host if connection.client else "unknown"


def peer_is_trusted_proxy(connection: HTTPConnection) -> bool:
    address = _ip(peer_address(connection))
    return address is not None and _within(address, trusted_proxies())


def _values(connection: HTTPConnection, name: str) -> list[str]:
    """Every comma-separated entry of a header, however many lines it came in."""
    return [item.strip() for line in connection.headers.getlist(name) for item in line.split(",") if item.strip()]


def forwarded_for(connection: HTTPConnection) -> list[Address] | None:
    """The ``X-Forwarded-For`` chain as addresses, or None when there is none, it is too long or any entry is not an address."""
    entries = _values(connection, "x-forwarded-for")
    if not entries or len(entries) > MAX_HOPS:
        return None
    chain = [_entry(entry) for entry in entries]
    return None if any(entry is None for entry in chain) else chain  # type: ignore[return-value]


def client_address(connection: HTTPConnection) -> str:
    """The caller's address: the peer's, or behind a trusted proxy the address that proxy reports."""
    peer = peer_address(connection)
    proxies = trusted_proxies()
    if proxies:
        chain = forwarded_for(connection) if peer_is_trusted_proxy(connection) else None
        if not chain:
            return peer
        return str(next((entry for entry in reversed(chain) if not _within(entry, proxies)), chain[0]))
    if legacy_forwarded_trust():
        entries = connection.headers.get("x-forwarded-for", "").split(",")
        if entries[-1].strip():
            return entries[-1].strip()[:64]
    return peer


def is_remote(address: str) -> bool:
    """Whether an address counts as remote (``classify``). One that cannot be read is treated as remote."""
    return classify(address)[0]


FORWARDED_HEADERS = ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "forwarded")


def has_forwarded_headers(connection: HTTPConnection) -> bool:
    return any(name in connection.headers for name in FORWARDED_HEADERS)


def request_is_remote(connection: HTTPConnection) -> bool:
    """Whether the caller is outside the local network, failing towards remote: forwarded headers from a peer that is not a trusted proxy come
    through a proxy the NVR was not told about, and a trusted proxy may report nothing usable, so in both the caller is unknown and counts as remote."""
    return request_class(connection)[0]


def request_class(connection: HTTPConnection) -> tuple[bool, str]:
    """``request_is_remote`` with the rule that decided it."""
    if trusted_proxies() and peer_is_trusted_proxy(connection):
        # A trusted proxy that does not say who the caller is (no header, one that is malformed or too long) leaves the caller unknown.
        return (True, "unknown_caller") if forwarded_for(connection) is None else classify(client_address(connection))
    remote, rule = classify(peer_address(connection))
    return (True, "unknown_caller") if not remote and has_forwarded_headers(connection) else (remote, rule)


def scheme(connection: HTTPConnection) -> str:
    """``https`` or ``http`` as the browser used it: from a trusted proxy's ``X-Forwarded-Proto``, else from the connection."""
    if peer_is_trusted_proxy(connection):
        proto = (_values(connection, "x-forwarded-proto") or [""])[-1].lower()
        if proto in {"http", "https"}:
            return proto
    return "https" if connection.url.scheme in {"https", "wss"} else "http"


def host(connection: HTTPConnection) -> str:
    """The host the browser asked for: from a trusted proxy's ``X-Forwarded-Host`` when it is a plausible host name, else the ``Host`` header."""
    if peer_is_trusted_proxy(connection):
        forwarded = (_values(connection, "x-forwarded-host") or [""])[-1].lower()
        if forwarded and _HOST.match(forwarded):
            return forwarded
    return connection.headers.get("host", "").strip().lower()


def origin(connection: HTTPConnection) -> str:
    return f"{scheme(connection)}://{host(connection)}"


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme in {"http", "https"} and parts.netloc else None


def allowed_origins() -> list[str]:
    """Origins besides the one a request was made to that may open the live WebSocket: ``NVR_PUBLIC_URL`` and ``NVR_ALLOWED_ORIGINS``."""
    listed = [_origin_of(item) for item in [os.environ.get("NVR_PUBLIC_URL", ""), *os.environ.get("NVR_ALLOWED_ORIGINS", "").split(",")] if item.strip()]
    return sorted({item for item in listed if item})


def origin_allowed(connection: HTTPConnection, origin_header: str) -> bool:
    """A browser's ``Origin`` is accepted when it names the host the request was made to (as a trusted proxy reports it) or an allowed origin."""
    candidate = _origin_of(origin_header)
    if candidate is None:
        return False
    return urlsplit(candidate).netloc == host(connection) or candidate in allowed_origins()


def describe(connection: HTTPConnection) -> dict[str, Any]:
    """How this request reached the NVR, for Settings: the facts, not yet the warnings."""
    proxies, invalid = parse_networks(os.environ.get("NVR_TRUSTED_PROXIES", ""))
    address = client_address(connection)
    remote, rule = request_class(connection)
    parsed, on_site = _ip(address), host_networks()
    return {
        "client_address": address, "peer_address": peer_address(connection), "peer_is_trusted_proxy": peer_is_trusted_proxy(connection),
        "remote": remote, "classed_by": rule, "classed_reason": REASONS[rule],
        "remote_networks": [str(network) for network in remote_networks()], "tunnel_networks": [str(network) for network in tunnel_networks()],
        "host_networks": [str(network) for network in on_site],
        # None when this machine's networks could not be read, or the address is not IPv4 (only IPv4 networks are read)
        "on_host_network": _within(parsed, on_site) if parsed is not None and parsed.version == 4 and on_site else None,
        "https": scheme(connection) == "https", "host": host(connection),
        "forwarded": {name: connection.headers.get(header, "")[:200] or None for name, header in
                      (("for", "x-forwarded-for"), ("proto", "x-forwarded-proto"), ("host", "x-forwarded-host"))},
        "trusted_proxies": [str(network) for network in proxies], "invalid_trusted_proxies": invalid, "legacy_forwarded_trust": legacy_forwarded_trust(),
        "local_networks": [str(network) for network in local_networks()], "allowed_origins": allowed_origins(),
        "public_url": os.environ.get("NVR_PUBLIC_URL", "").strip() or None,
    }


class TrustedProxyMiddleware:
    """Makes the scheme and host the application sees (for redirects, WebSocket checks and links) those a trusted proxy reports, and nobody else's."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in {"http", "websocket"} and trusted_proxies():
            connection = HTTPConnection(scope)
            if peer_is_trusted_proxy(connection):
                proto, wanted = scheme(connection), host(connection)
                scope = {**scope, "scheme": ({"https": "wss", "http": "ws"} if scope["type"] == "websocket" else {"https": "https", "http": "http"})[proto]}
                if wanted and wanted != connection.headers.get("host", "").strip().lower():
                    scope["headers"] = [(key, value) for key, value in scope["headers"] if key != b"host"] + [(b"host", wanted.encode("latin-1"))]
        await self.app(scope, receive, send)
