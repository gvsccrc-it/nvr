"""Idle connections cannot use up the web process (#546, the owner's decision on the ticket).

Uvicorn starts its idle timer only once an answer has been sent, so a connection that never sends a request was held for ever, and
nothing limited how many there were. Each holds a file handle in the web worker: about a thousand from one machine that had not
signed in stopped new connections and stopped the web worker opening the database.

Three rules, all applied as a connection is made, before anything is read from it:

- A connection that has not sent a whole first request (its request line and headers) within ``first_request_seconds`` is closed.
  A request's body may take longer: an upload is not cut off.
- At most ``max_connections`` are open at once. Live video sockets count; they are connections.
- At the limit, a new connection takes the place of the one that has waited longest without sending a request, if there is one.
  That keeps the health check answering through a flood of idle connections, so the container is not marked unhealthy and
  restarted for it. Only when every connection is doing real work is a new one turned away, and even then a few more are let in
  from this machine itself (``LOCAL_RESERVE``), which is where the health check comes from.

The limit is far above what the live-view limits allow anyone to use (32 streams, plus each tab's background requests) and well
below the open-file limit (``app.open_files``).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import ClassVar

from uvicorn.protocols.http.auto import AutoHTTPProtocol

from app.env_numbers import number, whole_number

logger = logging.getLogger("nvr.web.connections")
LOCAL_RESERVE = 16
SAY_EVERY_SECONDS = 60.0


def max_connections() -> int:
    return whole_number("NVR_WEB_MAX_CONNECTIONS", 512, 16, 16_384)


def first_request_seconds() -> float:
    return number("NVR_WEB_FIRST_REQUEST_SECONDS", 10, 1, 300)


def _this_machine(client: tuple[str, int] | None, server: tuple[str, int] | None) -> bool:
    """Whether a connection comes from this machine itself: from a loopback address, or from the very address it arrived at (the
    health check asks at the address the interface is served on, which need not be 127.0.0.1)."""
    if not client:
        return False
    if server and client[0] == server[0]:
        return True
    try:
        return ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


class GuardedProtocol(AutoHTTPProtocol):  # type: ignore[misc, valid-type]
    """Uvicorn's HTTP protocol with the three rules above. Everything else is uvicorn's."""

    # Connections that have not yet sent a whole first request, oldest first
    waiting: ClassVar[dict["GuardedProtocol", None]] = {}
    counts: ClassVar[dict[str, int]] = {"too_slow": 0, "displaced": 0, "refused": 0}
    _said_at: ClassVar[float] = 0.0

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        self._first_request: asyncio.TimerHandle | None = None
        limit = max_connections()
        if len(self.connections) > limit:
            oldest = next(iter(self.waiting), None)
            if oldest is not None:
                oldest._close("displaced")
            elif not _this_machine(self.client, self.server) or len(self.connections) > limit + LOCAL_RESERVE:
                self._close("refused")
                return
        self.waiting[self] = None
        self._first_request = self.loop.call_later(first_request_seconds(), self._close, "too_slow")

    def data_received(self, data: bytes) -> None:
        super().data_received(data)
        # A whole first request has arrived when uvicorn has started answering it, or has handed the connection to a WebSocket
        if self in self.waiting and (self.cycle is not None or self.transport.get_protocol() is not self):
            self._asked()

    def connection_lost(self, exc: Exception | None) -> None:
        self._asked()
        super().connection_lost(exc)

    def _asked(self) -> None:
        self.waiting.pop(self, None)
        timer, self._first_request = getattr(self, "_first_request", None), None
        if timer is not None:
            timer.cancel()

    def _close(self, why: str) -> None:
        self._asked()
        cls = type(self)
        cls.counts[why] += 1
        now = self.loop.time()
        if now - cls._said_at >= SAY_EVERY_SECONDS:  # One line a minute at most: a flood must not become a flood of log lines
            cls._said_at = now
            logger.warning("Closing connections that send no request or arrive past the limit of %d: %d sent nothing in time, %d made way for a "
                           "newer one, %d turned away, since the web worker started", max_connections(), cls.counts["too_slow"], cls.counts["displaced"], cls.counts["refused"])
        if not self.transport.is_closing():
            self.transport.close()
