"""Who searched for what: plate, attribute, natural-language and "find similar" searches go in the security log (#519).

The face search was logged; these were not, so the log could not show who looked up a vehicle's movements or followed a person from camera to
camera. As decided on the ticket: every one of the four is logged with the person, the kind, the query (the plate pattern, the words, the
filters, or the object the search started from), the cameras and the time range, never a picture or an embedding. The same person repeating
the same query within a minute adds no row. The rows are in the security log, which only administrators read, and follow its retention.

The write is handed to a thread of its own, so the search answers without waiting for the database's write lock (a search box may ask on
every keystroke).
"""
from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .clock import stamp
from .store import connect, database_path

REPEAT_WINDOW = timedelta(minutes=1)
KINDS = {"plate": "search_plate", "attributes": "search_attributes", "words": "search_words", "similar": "search_similar"}

_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-audit")
_log = logging.getLogger("nvr.search_audit")


def _key(query: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(query, sort_keys=True, default=str).encode()).hexdigest()[:16]


def write(actor_id: int, kind: str, query: dict[str, Any], now: datetime | None = None, database: Path | None = None) -> bool:
    """Record one search, unless the same person ran the same query within ``REPEAT_WINDOW``. Returns whether a row was written."""
    from .audit import record

    action, key = KINDS[kind], _key(query)
    since = stamp((now or datetime.now(timezone.utc)) - REPEAT_WINDOW)
    with connect(database) as db:
        if db.execute("SELECT 1 FROM audit_log WHERE actor_id=? AND action=? AND resource_id=? AND created_at>? LIMIT 1", (actor_id, action, key, since)).fetchone():
            return False  # Read first: a repeat takes no write lock (#280)
        record(db, actor_id, action, "search", key, **query)
    return True


def log(user: Any, kind: str, query: dict[str, Any]) -> None:
    """Record the search after the request has answered. ``query`` holds only what was asked: never a picture or an embedding."""
    database = database_path()  # Now, in the request: the write happens a moment later on another thread

    def run() -> None:
        try:
            write(int(user["id"]), kind, query, database=database)
        except Exception:  # noqa: BLE001 - the search has been answered; a log row that could not be written is said in the server log
            _log.exception("A %s search could not be written to the security log", kind)

    _writer.submit(run)


def flush(timeout: float = 5.0) -> None:
    """Wait for the writes handed over so far (for tests and shutdown)."""
    _writer.submit(lambda: None).result(timeout=timeout)
