"""Is the database file itself damaged? (#321)

A damaged file used to show only as a generic "try again shortly" on every page and a loop of tracebacks in the workers. This module
answers the question directly, for three callers: the nightly self-test (the full ``quick_check``, which reads every page and takes a few
minutes on a large file), a light probe at startup and for the self-test run by hand (it opens the file and reads the start of every
table, which takes milliseconds), and the web error handler, which recognises a damaged file by its error.

Nothing here writes to the database or takes its write lock. The full check holds one read snapshot while it runs, so the write-ahead log
cannot be reset until it finishes; it runs in the background worker, at night.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .store import connect

logger = logging.getLogger(__name__)

RECOVERY_DOCUMENT = "docs/database-recovery.md"
ADVICE = (
    "Stop the NVR and do not delete anything. Copy the data folder, then restore the newest healthy copy "
    f"(the *.pre-migration-* files next to the database, or a configuration backup): see {RECOVERY_DOCUMENT}."
)
_LINES_SHOWN = 5


@dataclass
class Outcome:
    ok: bool
    full: bool
    seconds: float
    lines: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return "; ".join(self.lines[:_LINES_SHOWN]) if self.lines else ("passed" if self.ok else "failed")


def is_damage(error: BaseException) -> bool:
    """A damaged or unreadable file, as opposed to a busy one: SQLite reports corruption and a file that is not a database as a plain
    ``DatabaseError``, while a locked database, a constraint or a bad statement are its subclasses."""
    return type(error) is sqlite3.DatabaseError


def _open(path: Path | str | None) -> sqlite3.Connection:
    if path is None:
        return connect()
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    return db


def check(*, full: bool, path: Path | str | None = None, lines_shown: int = _LINES_SHOWN) -> Outcome:
    """Run the full ``quick_check`` or the light probe on the live database (or on ``path``, a copy)."""
    started = time.monotonic()
    try:
        db = _open(path)
        try:
            if full:
                found = [str(row[0]) for row in db.execute(f"PRAGMA quick_check({int(lines_shown)})").fetchall()]
                lines = [] if found == ["ok"] else found
            else:
                lines = _probe(db)
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        if not is_damage(exc):
            raise  # Busy, out of disk or a bug: not a statement about the file, so the caller decides
        lines = [str(exc)]
    return Outcome(ok=not lines, full=full, seconds=round(time.monotonic() - started, 2), lines=lines)


def _probe(db: sqlite3.Connection) -> list[str]:
    """Open the file and read the first row of every table: finds a file that is not a database, a broken schema and a damaged first page of any table."""
    db.execute("PRAGMA schema_version").fetchone()
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        db.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone()
    return []


def log_at_startup() -> bool:
    """One clear line in the log when the file is damaged, instead of a traceback from every worker. Returns True when the file is fine."""
    try:
        outcome = check(full=False)
    except sqlite3.Error:
        logger.exception("The startup database check could not run")
        return True
    if outcome.ok:
        return True
    logger.error("THE DATABASE FILE IS DAMAGED (%s). %s", outcome.summary, ADVICE)
    report_damage(sqlite3.DatabaseError(outcome.summary), "startup check")
    return False


# --- damage found while running (#453) ---------------------------------------------------------------------------------------------------
#
# The light probe reads the start of every table, so damage deeper in a table passes it: on the reporting site the container stayed healthy
# for 16 hours while the recorder and AI workers failed on every start and every background loop logged a traceback on every pass. Whoever
# meets damage now says so in one shared place, a file beside the workers' liveness files: the health check fails while it is there, the
# supervisor stops restarting workers in a tight loop, and each process logs one clear line instead of a traceback per pass. The file is
# cleared when the NVR starts again (after the database is restored); damage that is still there is found again within moments.

DAMAGED_EXIT = 3  # A worker's exit code when it stopped because the database file is damaged
SUMMARY_SECONDS = 300.0  # While damaged, each process logs one reminder this often, with how many errors it left out


def _marker() -> Path:
    from .workers import control_directory  # Late: workers imports the store

    return control_directory() / "database-damaged.json"


def report_damage(error: BaseException, where: str) -> None:
    """Record that the database file is damaged (the first report is kept). Never raises: it runs on the way out of a failure."""
    try:
        marker = _marker()
        if marker.exists():
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        partial = marker.with_name(f".{marker.name}.{os.getpid()}")
        partial.write_text(json.dumps({"found_at": datetime.now(timezone.utc).isoformat(), "where": where, "error": str(error)[:300]}), encoding="utf-8")
        os.replace(partial, marker)
    except OSError:
        logger.warning("Could not record that the database file is damaged")


def damage_reported() -> dict | None:
    """What was recorded when damage was found, or None."""
    try:
        return json.loads(_marker().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_damage_report() -> None:
    """At start-up: a new start (after a restore) begins clean; damage still in the file is reported again as soon as it is read."""
    try:
        _marker().unlink(missing_ok=True)
    except OSError:
        pass


def _damage_in(record: logging.LogRecord) -> BaseException | None:
    error = record.exc_info[1] if record.exc_info else None
    while error is not None:
        if isinstance(error, BaseException) and is_damage(error):
            return error
        error = error.__cause__ or error.__context__
    return None


class DamageLogFilter(logging.Filter):
    """Turns a traceback caused by a damaged database into one clear line, the first time in a process, then one reminder every
    ``SUMMARY_SECONDS`` with how many it left out. A loop that meets the damage on every pass no longer fills the log with tracebacks."""

    def __init__(self, process: str) -> None:
        super().__init__()
        self.process = process
        self.announced = False
        self.suppressed = 0
        self.reminded = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        decided = getattr(record, "_damage_decision", None)
        if decided is not None:  # Already seen by this filter on another handler
            return decided
        error = _damage_in(record)
        if error is None:
            return True
        record._damage_decision = self._decide(record, error)
        return record._damage_decision

    def _decide(self, record: logging.LogRecord, error: BaseException) -> bool:
        report_damage(error, f"{self.process}: {record.name}")
        now = time.monotonic()
        if self.announced and now - self.reminded < SUMMARY_SECONDS:
            self.suppressed += 1
            return False
        if self.announced:
            record.msg, record.args = "The database file is still damaged (%d more errors not logged). %s", (self.suppressed, ADVICE)
        else:
            record.msg, record.args = "THE DATABASE FILE IS DAMAGED (%s, in %s). %s", (error, record.name, ADVICE)
        record.exc_info, record.exc_text, record.stack_info, record.levelno, record.levelname = None, None, None, logging.ERROR, "ERROR"
        self.announced, self.suppressed, self.reminded = True, 0, now
        return True


def install_log_filter(process: str) -> DamageLogFilter:
    """On every handler of the root logger, so whatever logs a damage error is caught."""
    damage_filter = DamageLogFilter(process)
    for handler in logging.getLogger().handlers:
        handler.addFilter(damage_filter)
    return damage_filter
