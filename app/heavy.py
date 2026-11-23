"""Requests that can read a great deal (Insights, the heatmap, attribute and similar-looking searches, a rule's test) are limited (#515).

They are synchronous routes: each ran to the end on one of the web server's threads, even after the browser had gone, with no limit on how many
ran at once and no time budget. Any signed-in operator could tie up the thread pool, and a long read stops the database's log from being reset.

Now at most ``AT_ONCE`` run at once in a process; another is answered at once with 503 and "try again", never queued behind them. Each has
``BUDGET_SECONDS``: every database connection the request opens (``store.connect`` asks ``deadline()``) is stopped by SQLite once the time is up,
and the request is answered with 503 saying the range was too large.
"""
from __future__ import annotations

import contextvars
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException

from .env_numbers import number

AT_ONCE = number("NVR_HEAVY_REQUESTS", 4, 1, 64, whole=True)
BUDGET_SECONDS = number("NVR_HEAVY_REQUEST_SECONDS", 30, 1, 600)
CHECK_EVERY = 20_000  # SQLite steps between looks at the clock: a few milliseconds of work

_slots = threading.BoundedSemaphore(AT_ONCE)
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("heavy_deadline", default=None)


class TooLong(Exception):
    pass


def deadline() -> float | None:
    """When the request this thread is answering must stop, by ``time.monotonic()``; None outside a limited request."""
    return _deadline.get()


def watch(db: sqlite3.Connection) -> None:
    """Have SQLite stop this connection's statement once the request's time is up (``store.connect`` calls this for every connection)."""
    until = _deadline.get()
    if until is not None:
        db.set_progress_handler(lambda: 1 if time.monotonic() > until else 0, CHECK_EVERY)


@contextmanager
def limited(what: str, seconds: float | None = None) -> Iterator[None]:
    """Run the body as one of the limited requests: refused with 503 when ``AT_ONCE`` are running, stopped with 503 past its time."""
    if not _slots.acquire(blocking=False):
        raise HTTPException(status_code=503, detail=f"The server is busy with other searches and reports; try the {what} again in a moment",
                            headers={"Retry-After": "5"})
    budget = BUDGET_SECONDS if seconds is None else seconds
    token = _deadline.set(time.monotonic() + budget)
    try:
        yield
        if time.monotonic() > _deadline.get():  # Past its time in Python, after the database had answered: still too much
            raise TooLong
    except (TooLong, sqlite3.OperationalError) as exc:
        if isinstance(exc, sqlite3.OperationalError) and "interrupted" not in str(exc):
            raise
        raise HTTPException(status_code=503, detail=f"The {what} took longer than {budget:g} seconds and was stopped: choose fewer cameras or a shorter time") from None
    finally:
        _deadline.reset(token)
        _slots.release()
