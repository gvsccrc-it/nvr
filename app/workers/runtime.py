"""Shared lifecycle for worker processes: signals, control socket and heartbeat."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sqlite3
import sys
from typing import Protocol

from app import db_health, logforward
from app.workers import control, liveness


class Service(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def handle(self, message: dict[str, object]) -> None: ...


async def run(name: str, service: Service) -> None:
    os.umask(0o077)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await service.start()
    server = await control.serve(name, service.handle)
    heartbeat = asyncio.create_task(liveness.heartbeat_forever(name), name=f"{name}-heartbeat")
    lag = asyncio.create_task(liveness.watch_loop_lag(name), name=f"{name}-loop-lag")
    try:
        await stop.wait()
    finally:
        server.close()
        heartbeat.cancel()
        lag.cancel()
        await service.stop()


def main(name: str, service: Service) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logforward.install(name)
    db_health.install_log_filter(name)
    try:
        asyncio.run(run(name, service))
    except sqlite3.DatabaseError as exc:
        if not db_health.is_damage(exc):
            raise
        # The file is damaged: say so once and exit with the code that tells the supervisor not to restart this worker in a tight loop (#453).
        db_health.report_damage(exc, f"{name} worker")
        logging.getLogger(f"nvr.{name}").error("The %s worker stopped: THE DATABASE FILE IS DAMAGED (%s). %s", name, exc, db_health.ADVICE)
        sys.exit(db_health.DAMAGED_EXIT)
