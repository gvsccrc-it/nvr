"""Serves the failover link over HTTPS, on a port of its own, while failover is on (#502, part 2).

The web interface is plain HTTP on ``NVR_LISTEN_PORT`` (HTTPS comes from a tunnel or proxy in front of it), so the endpoints the two NVRs
call on each other are also served, alone, by a second server in the web worker: TLS with this NVR's own link certificate
(``app.failover_link``) on ``NVR_FAILOVER_LINK_PORT`` (8443 unless set; 0 turns it off), at ``NVR_FAILOVER_LINK_HOST`` (the interface's
address unless set). It runs in a thread of its own with its own event loop, so the work of encrypting footage during a handback is never
done on the loop that serves live video, and it shares the web process's memory of one-time values, so a request is refused if it is sent
again to either port.

It listens only while this NVR's failover role is not "off" (an NVR that is not in a pair opens no port for it), and it starts again with
the new certificate when one is made. What it reads to decide is read-only: nothing here takes the database's write lock.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import socket
import sqlite3
import threading
from typing import Any

import uvicorn

from . import failover_link
from .store import connect, database_path

logger = logging.getLogger("nvr.failover")

CHECK_SECONDS = 5.0


def link_port() -> int | None:
    """The port the link is served on, or None when it is turned off (``NVR_FAILOVER_LINK_PORT=0``) or set to something that is not a port."""
    raw = os.environ.get("NVR_FAILOVER_LINK_PORT", "").strip() or str(failover_link.DEFAULT_PORT)
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def link_host() -> str:
    """The address the link is served on: ``NVR_FAILOVER_LINK_HOST``, else the interface's own (``NVR_LISTEN_HOST``)."""
    from .entrypoint import listen_host

    raw = os.environ.get("NVR_FAILOVER_LINK_HOST", "").strip()
    if raw:
        return str(ipaddress.ip_address(raw))
    return listen_host()


def link_app():
    """Only the failover endpoints: nothing else of the NVR is reachable on the link's port."""
    from fastapi import FastAPI

    from .api.failover import inbound_router

    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.include_router(inbound_router)
    return application


class _LinkServer(uvicorn.Server):
    def capture_signals(self):  # The web server handles the process's signals; this one is stopped by the web worker
        return contextlib.nullcontext()


def _wanted() -> tuple[str, int, str] | None:
    """Where the link should be served, and with which certificate, or None when it should not be."""
    port = link_port()
    if port is None or not database_path().exists():
        return None
    try:
        with connect() as db:
            row = db.execute("SELECT role FROM failover_settings WHERE singleton_id=1").fetchone()
    except sqlite3.OperationalError:
        return None  # A database from before failover
    if row is None or row["role"] == "off":
        return None
    return link_host(), port, failover_link.certificate()[1]


def _bind(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host, port), family=family, backlog=64)


class FailoverLink:
    """The link's listener, started and stopped by the web worker."""

    def __init__(self, check_seconds: float = CHECK_SECONDS) -> None:
        self.check_seconds = check_seconds
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self.serving: tuple[str, int, str] | None = None  # (host, port, fingerprint) while it listens
        self._said: str | None = None

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="failover-link", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:  # noqa: BLE001 - the web interface goes on; the link is down and the log says why
            logger.exception("The failover link's listener stopped")

    def _say(self, message: str, *args: Any) -> None:
        text = message % args
        if text != self._said:  # Once per distinct problem, not every few seconds
            self._said = text
            logger.error("%s", text)

    async def _wait(self, seconds: float) -> bool:
        return await asyncio.to_thread(self._stopping.wait, seconds)

    async def _main(self) -> None:
        server: _LinkServer | None = None
        task: asyncio.Task[None] | None = None
        if await self._wait(1.0):  # A moment for the web server to start first (and nothing at all for a worker stopped at once)
            return
        while True:
            try:
                wanted = await asyncio.to_thread(_wanted)
            except Exception as exc:  # noqa: BLE001 - kept as it is, and tried again at the next check
                self._say("The failover link could not be prepared (%s): check NVR_FAILOVER_LINK_HOST and NVR_FAILOVER_LINK_PORT", type(exc).__name__)
                wanted = self.serving
            if task is not None and task.done():
                server, task, self.serving = None, None, None
            if wanted != self.serving:
                if server is not None and task is not None:
                    server.should_exit = True
                    await asyncio.wait({task}, timeout=10)
                server, task, self.serving = None, None, None
                if wanted is not None:
                    server, task = await self._serve(*wanted)
                    if task is not None:
                        self.serving = wanted
            if await self._wait(self.check_seconds):
                break
        if server is not None and task is not None:
            server.should_exit = True
            await asyncio.wait({task}, timeout=10)
        self.serving = None

    async def _serve(self, host: str, port: int, fingerprint: str) -> tuple[_LinkServer | None, asyncio.Task[None] | None]:
        from .entrypoint import listen_port

        try:
            if port == listen_port():
                self._say("NVR_FAILOVER_LINK_PORT is the web interface's own port (%s): set it to a port of its own; the failover link is not served", port)
                return None, None
        except SystemExit:
            pass
        try:
            context, _ = await asyncio.to_thread(failover_link.server_context)
            sock = _bind(host, port)
        except OSError as exc:
            self._say("The failover link could not listen on %s port %s (%s): set NVR_FAILOVER_LINK_PORT to a free port", host, port, exc.strerror or type(exc).__name__)
            return None, None
        config = uvicorn.Config(link_app(), host=host, port=port, lifespan="off", log_config=None, access_log=False, proxy_headers=False, server_header=False,
                                timeout_graceful_shutdown=5, limit_concurrency=32, ssl_context_factory=lambda _config, _default: context)
        server = _LinkServer(config)
        task = asyncio.create_task(server.serve(sockets=[sock]), name="failover-link")
        self._said = None
        logger.info("The failover link is served over HTTPS on %s port %s (certificate %s)", host, port, failover_link.display(fingerprint))
        return server, task
