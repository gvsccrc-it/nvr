"""The web worker: the HTTP API and browser interface."""
from __future__ import annotations

import copy
import logging

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from app import db_health, logforward, open_files
from app.entrypoint import listen_host, listen_port


class FailedRequestsOnly(logging.Filter):
    """Keeps the access log's lines for requests that failed (status 400 and above) and drops the rest (#543).

    The access log wrote a line for every request: the health check every 30 seconds and 40 to 100 background requests a minute from
    each open browser tab, all of it into the container's log on the system disk. What went wrong is what the log is read for."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return int(record.args[4]) >= 400  # type: ignore[index]  # (client, method, path, HTTP version, status)
        except (TypeError, ValueError, IndexError, KeyError):
            return True  # Not a line of the usual shape: kept


def log_config() -> dict:
    """Uvicorn's own logging, with its server and error log (start-up failures, "Exception in ASGI application") also passed up to the
    root logger, where log forwarding listens. The access log stays out of that, and holds only the requests that failed."""
    config = copy.deepcopy(LOGGING_CONFIG)
    config["loggers"]["uvicorn"]["propagate"] = True
    config.setdefault("filters", {})["failed_requests_only"] = {"()": "app.workers.web.FailedRequestsOnly"}
    config["handlers"]["access"]["filters"] = ["failed_requests_only"]
    return config


def main() -> None:
    logforward.install("web")
    db_health.install_log_filter("web")  # A background task of the web process that meets a damaged database says so once and fails the health check (#453)
    # Forwarded headers are believed only from NVR_TRUSTED_PROXIES (app.network), never by uvicorn on its own: its default trusts any 127.0.0.1 sender.
    open_files.raise_limit()
    # The protocol closes connections that send no request and holds the number of connections to a limit (app.connection_guard, #546).
    uvicorn.run("app.main:app", host=listen_host(), port=listen_port(), timeout_graceful_shutdown=10, log_config=log_config(), proxy_headers=False,
                http="app.connection_guard:GuardedProtocol")


if __name__ == "__main__":
    main()
