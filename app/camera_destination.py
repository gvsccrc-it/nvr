"""Where the NVR may connect for a camera (#474): one rule for every camera address, checked on the address actually connected to.

Inspecting a camera, Bulk Add, CSV import, the stream test, recording and every use of a stored device address (PTZ, masks, relay
outputs, two-way audio, health, events) connect to an address an administrator, a file or a camera gave. Without a rule, an ``admin``
token could use them to map the network the NVR sits on, the NVR's own loopback services and a cloud metadata address included. So:

* **Never**, with no override: link-local addresses (``169.254.0.0/16``, ``fe80::/10``, which hold cloud metadata services), unspecified
  (``0.0.0.0/8``, ``::``), multicast and reserved addresses, and the NVR's own addresses other than loopback.
* **Loopback** (``127.0.0.0/8``, ``::1``): refused unless the server's environment sets ``NVR_CAMERA_ALLOW_LOOPBACK=true``, for a camera fed
  through a restreamer on the same machine (and for the tests and CI, whose stand-in cameras listen there). It is environment only, so a
  leaked token cannot turn it on; the hardening page warns while it is on.
* **Private addresses**, every address the internet does not route (``10/8``, ``172.16/12``, ``192.168/16``, ``100.64/10``, ``fc00::/7``
  and the like): allowed.
* **Public addresses** (``ipaddress``'s ``is_global``): refused unless the camera is allowed to be at a public address
  (``cameras.public_address``), which only an administrator signed in with a browser can tick, one camera at a time.

A host name is looked up once, every address it gives must pass, and the connection goes to the checked address: a name cannot pass the
check and then be looked up again to somewhere else. HTTP clients get a transport that does this at connect time (the Host header and TLS
keep the name); FFmpeg is given the checked address in place of the name (``pin_url``).

Messages never hold the address or a login: they are stored and shown to operators (#471).
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx

LOOPBACK_SETTING = "NVR_CAMERA_ALLOW_LOOPBACK"
THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")  # Linux connects 0.x.x.x to the machine itself
DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}
PUBLIC_TICK = "This camera is at a public address"

MESSAGES = {
    "unspecified": "A camera address must be one device's address, not an unspecified address",
    "multicast": "A camera address must be one device's address, not a multicast address",
    "reserved": "A camera address cannot be a reserved address",
    "link_local": "A camera address cannot be a link-local address (169.254.x.x or fe80::), which is where cloud metadata services answer",
    "own": "A camera address cannot be one of the NVR's own addresses",
    "loopback": ("A camera address cannot be on the NVR's own machine (loopback). If cameras reach the NVR through a restreamer on the same "
                 f"machine, whoever runs the server can set {LOOPBACK_SETTING}=true in its configuration"),
    "public": (f"This camera's address is a public internet address. An administrator signed in with a browser can allow it for this camera by "
               f"ticking \"{PUBLIC_TICK}\""),
}

_public_permitted: ContextVar[bool] = ContextVar("camera_public_address_permitted", default=False)


class RefusedDestination(ValueError):
    """A camera address the rule refuses. ``kind`` is one of ``MESSAGES``; the message is safe to show and holds no address."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(MESSAGES[kind])


def loopback_allowed() -> bool:
    """Whether the server's own configuration lets cameras be on its loopback addresses."""
    return os.environ.get(LOOPBACK_SETTING, "").strip().lower() in {"1", "true", "yes"}


@contextmanager
def public_permitted(allowed: bool) -> Iterator[None]:
    """For one piece of work (checking or saving one camera): whether that camera may be at a public address. Calls that are not told
    otherwise follow it; outside it, public addresses are refused."""
    token = _public_permitted.set(bool(allowed))
    try:
        yield
    finally:
        _public_permitted.reset(token)


def permitted() -> bool:
    """Whether the work in progress may reach a camera at a public address (``public_permitted``)."""
    return _public_permitted.get()


def _permitted(allow_public: bool | None) -> bool:
    return _public_permitted.get() if allow_public is None else bool(allow_public)


def camera_allows_public(row: Any) -> bool:
    """Whether a stored camera may be at a public address: allowed with the tick, or kept from before the rule (``grandfathered``)."""
    return row["public_address"] is not None


def _address(value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(str(value).split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The host as an IP address, or None when it is a name."""
    try:
        return _address(host.strip().strip("[]"))
    except ValueError:
        return None


def _binds_anywhere(family: socket.AddressFamily) -> bool:
    """Whether this machine lets a socket bind to an address it does not have (``ip_nonlocal_bind``): then binding cannot tell its own."""
    probe = "192.0.2.1" if family == socket.AF_INET else "2001:db8::1"
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind((probe, 0))
        return True
    except OSError:
        return False


def is_own(address: Any) -> bool:
    """Whether the address is one of this machine's: only a machine's own address can be bound to."""
    address = _address(address)
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind((str(address), 0))
    except OSError:
        return False
    return not _binds_anywhere(family)


def classify(value: Any) -> str:
    """``unspecified``, ``loopback``, ``link_local``, ``multicast``, ``reserved``, ``own``, ``private`` or ``public``."""
    address = _address(value)
    if address.is_unspecified or address in THIS_NETWORK:
        return "unspecified"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link_local"
    if address.is_multicast:
        return "multicast"
    if address.is_reserved:
        return "reserved"
    if is_own(address):
        return "own"
    return "public" if is_global(address) else "private"


def is_global(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether the internet routes the address. (The tests replace this to stand a documentation range in for the internet: no real public
    address appears in this repository.)"""
    return address.is_global


def check(value: Any, *, allow_public: bool | None = None) -> str:
    """The address's kind when a camera may be reached there; ``RefusedDestination`` saying why not otherwise."""
    kind = classify(value)
    if kind == "private" or kind == "public" and _permitted(allow_public) or kind == "loopback" and loopback_allowed():
        return kind
    raise RefusedDestination(kind)


def addresses_of(host: str, port: int | None = None) -> list[str]:
    """Every address a host name gives (the address itself for a literal one). ``OSError`` when it cannot be looked up."""
    found = literal(host)
    if found is not None:
        return [str(found)]
    infos = socket.getaddrinfo(host.strip().strip("[]"), port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(str(_address(info[4][0])) for info in infos))
    if not addresses:
        raise socket.gaierror("The camera's host name gave no address")
    return addresses


def resolve(host: str, port: int | None = None, *, allow_public: bool | None = None) -> str:
    """The address to connect to for ``host``: looked up once, every address it gives checked, and one of them (IPv4 first) returned.
    ``RefusedDestination`` when any address is refused; ``OSError`` when the name cannot be looked up."""
    addresses = addresses_of(host, port)
    for address in addresses:
        check(address, allow_public=allow_public)
    return sorted(addresses, key=lambda item: _address(item).version)[0]


async def aresolve(host: str, port: int | None = None, *, allow_public: bool | None = None) -> str:
    """``resolve`` off the event loop (a name lookup can take seconds)."""
    if literal(host) is not None:
        return resolve(host, port, allow_public=allow_public)
    return await asyncio.to_thread(resolve, host, port, allow_public=_permitted(allow_public))


def pin_url(url: str, *, allow_public: bool | None = None) -> str:
    """``url`` with its host replaced by the checked address to connect to, keeping the login, port, path and query exactly as written.
    For FFmpeg, so it cannot look the name up again and reach somewhere else. Something with no host (a file the tests stand in for a camera
    with) has nothing to connect to and is returned as it is: FFmpeg is only ever allowed camera protocols outside the tests."""
    parts = urlsplit(url.strip())
    if not parts.hostname:
        return url
    port = parts.port
    address = resolve(parts.hostname, port or DEFAULT_PORTS.get(parts.scheme), allow_public=allow_public)
    login = parts.netloc.rpartition("@")[0] if "@" in parts.netloc else ""
    host = f"[{address}]" if ":" in address else address
    netloc = (f"{login}@" if login else "") + host + (f":{port}" if port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def apin_url(url: str, *, allow_public: bool | None = None) -> str:
    """``pin_url`` off the event loop."""
    host = urlsplit(url.strip()).hostname or ""
    if literal(host) is not None:
        return pin_url(url, allow_public=allow_public)
    return await asyncio.to_thread(pin_url, url, allow_public=_permitted(allow_public))


def host_kind(host: str) -> str | None:
    """For a list: the kind of a host's addresses, the least safe first (``public`` before ``private``), or None when it cannot be looked
    up. Never raises."""
    try:
        kinds = {classify(address) for address in addresses_of(host)}
    except (OSError, ValueError, UnicodeError):
        return None
    order = ("unspecified", "link_local", "multicast", "reserved", "own", "loopback", "public", "private")
    return next(kind for kind in order if kind in kinds)


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    """httpcore's network backend with the rule at connect time: the name is looked up and checked here, and the connection is made to the
    checked address. TLS still checks the certificate against the name (httpcore passes the request's host as the server name)."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend, allow_public: bool) -> None:
        self._inner, self._allow_public = inner, allow_public

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        try:
            address = await aresolve(host, port, allow_public=self._allow_public)
        except RefusedDestination:
            raise
        except (OSError, UnicodeError) as exc:
            raise httpcore.ConnectError("The camera's host name could not be looked up") from exc
        return await self._inner.connect_tcp(address, port, timeout=timeout, local_address=local_address, socket_options=socket_options)

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        raise RefusedDestination("reserved")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def camera_transport(*, allow_public: bool | None = None) -> httpx.AsyncHTTPTransport:
    """An httpx transport for a camera: every connection it makes follows the rule. Whether the camera may be public is fixed when it is made."""
    transport = httpx.AsyncHTTPTransport()
    pool = transport._pool  # noqa: SLF001 - httpx 0.28 gives no other way to hand httpcore a network backend
    if not isinstance(pool, httpcore.AsyncConnectionPool) or not hasattr(pool, "_network_backend"):
        raise RuntimeError("The HTTP library changed: camera connections could not be guarded")
    pool._network_backend = _GuardedBackend(pool._network_backend, _permitted(allow_public))  # noqa: SLF001
    return transport


def camera_client(*, allow_public: bool | None = None, **options: Any) -> httpx.AsyncClient:
    """An httpx client for a camera's own services (PTZ, masks, relay outputs): no proxy from the environment, no redirects, the rule on
    every connection."""
    return httpx.AsyncClient(trust_env=False, follow_redirects=False, transport=camera_transport(allow_public=allow_public), **options)
