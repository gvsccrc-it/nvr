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


# --- the patrol: lost pages noticed within minutes (#455) ---------------------------------------------------------------------------------
#
# Twice the virtual disk under the reporting site returned 4 KiB of zeros where a page of the most rewritten tables had been. The light
# probe cannot see that and the full check runs once a night, so the damage was found by whatever read the page next. Every few minutes the
# background worker now checks the structure of the hot tables, and then of as many of the others, in turn, as fit in a few seconds, so
# the whole file is gone over many times a day. It only reads: a table's check holds a read snapshot for as long as it runs.

HOT_TABLES = ("cameras", "coverage_intervals")  # Rewritten all day: where pages were lost both times
PATROL_SECONDS = 300.0  # Between passes
PATROL_BUDGET_SECONDS = 3.0  # Once the hot tables are done, further tables are started only while the pass has taken less than this
PATROL_TABLE_SECONDS = 20.0  # One table's check is given up after this (the nightly check still reads it all)
_PROGRESS_STEPS = 50_000


class Patrol:
    def __init__(self) -> None:
        self._next = 0  # Where the round of the other tables continues
        self._last = float("-inf")

    def due(self, now: float | None = None) -> bool:
        return (time.monotonic() if now is None else now) - self._last >= PATROL_SECONDS

    def run(self, path: Path | str | None = None) -> Outcome:
        """Check the hot tables and the next few others. ``Outcome.lines`` holds what was found, each line starting with its table."""
        started = time.monotonic()
        self._last = started
        lines: list[str] = []
        try:
            db = _open(path)
            try:
                names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
                hot = [name for name in HOT_TABLES if name in names]
                others = [name for name in names if name not in hot]
                for name in hot:
                    lines += self._table(db, name)
                for done in range(len(others)):
                    if lines or (done and time.monotonic() - started >= PATROL_BUDGET_SECONDS):
                        break  # At least one a pass, so the round always moves on
                    self._next %= len(others)
                    lines += self._table(db, others[self._next])
                    self._next += 1
            finally:
                db.close()
        except sqlite3.DatabaseError as exc:
            if not is_damage(exc):
                raise
            lines.append(str(exc))
        return Outcome(ok=not lines, full=False, seconds=round(time.monotonic() - started, 2), lines=lines[:_LINES_SHOWN])

    @staticmethod
    def _table(db: sqlite3.Connection, name: str) -> list[str]:
        deadline = time.monotonic() + PATROL_TABLE_SECONDS
        db.set_progress_handler(lambda: time.monotonic() > deadline, _PROGRESS_STEPS)
        try:
            found = [str(row[0]) for row in db.execute(f'PRAGMA quick_check("{name}")').fetchall()]
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                return []  # Too large for a pass: left to the nightly check
            raise
        except sqlite3.DatabaseError as exc:
            if not is_damage(exc):
                raise
            return [f"{name}: {exc}"]
        finally:
            db.set_progress_handler(None, 0)
        return [] if found == ["ok"] else [f"{name}: {' '.join(line.split())}" for line in found]


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
