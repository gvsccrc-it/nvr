from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def database_path() -> Path:
    return Path(os.environ.get("NVR_DB_PATH", "./data/nvr.sqlite3"))


class _Connection(sqlite3.Connection):
    """``with connect() as db`` commits or rolls back and then closes. sqlite3's own context manager leaves the connection open until it
    is garbage collected, so every request left two to four of them behind (#278)."""

    def __exit__(self, *exc_info: object) -> bool | None:
        try:
            return super().__exit__(*exc_info)
        finally:
            self.close()


_prepared: set[str] = set()


def _synchronous() -> str:
    # WAL with NORMAL never corrupts the database; a power cut can lose the last few commits, where FULL waits for the disk on every one.
    value = os.environ.get("NVR_DB_SYNCHRONOUS", "NORMAL").strip().upper()
    return value if value in {"OFF", "NORMAL", "FULL", "EXTRA"} else "NORMAL"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """The database (``database_path()`` unless ``path`` names it: work handed to another thread keeps the file it was meant for)."""
    path = path or database_path()
    key = str(path)
    if key not in _prepared or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None, factory=_Connection)
    if key not in _prepared or not path.exists():
        os.chmod(path, 0o600)
        _prepared.add(key)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute(f"PRAGMA synchronous = {_synchronous()}")
    db.execute(f"PRAGMA journal_size_limit = {log_limit_bytes()}")
    from . import heavy

    heavy.watch(db)  # Inside a limited request (Insights, search, a rule's test), stopped when its time is up (#515)
    return db


def log_limit_bytes() -> int:
    """The most the write-ahead log file is left at once it can be reset (NVR_DB_LOG_LIMIT_MB, 128 by default, 16 to 4096). With no limit
    the file kept its largest-ever size: a long read (the nightly check, a backup) holds the log open, it grows with every write
    meanwhile, and nothing made it smaller again while any process had the database open, which is always (#493)."""
    try:
        megabytes = int(os.environ.get("NVR_DB_LOG_LIMIT_MB", "128"))
    except ValueError:
        megabytes = 128
    return max(16, min(megabytes, 4096)) << 20


def log_bytes() -> int:
    """The size of the write-ahead log file now."""
    try:
        return database_path().with_name(database_path().name + "-wal").stat().st_size
    except OSError:
        return 0


def shrink_log() -> tuple[int, int]:
    """Move everything in the write-ahead log into the database and empty the log file, if nothing is reading it; returns the file's size
    before and after. Run after the long reads (the nightly check, a backup). Never raises for a busy database: the log is left as it is."""
    before = log_bytes()
    try:
        db = sqlite3.connect(database_path(), timeout=2, isolation_level=None)
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        pass
    return before, log_bytes()


def has_row(db: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> bool:
    """Whether a query finds anything, read without a transaction. The background loops that claim queued work ask this before
    ``BEGIN IMMEDIATE``, so an idle loop never takes the database's one write lock (#280). The query may be wider than the claim's own; it
    must never be narrower, or work would wait."""
    return db.execute(sql, params).fetchone() is not None


def read_one(sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
    """One row, on a connection of its own. For an asynchronous web handler, which must not touch the database on the event loop (#354):
    ``row = await asyncio.to_thread(read_one, "SELECT ...", (camera_id,))``."""
    with connect() as db:
        return db.execute(sql, params).fetchone()


def initialize() -> None:
    from app.migrations import migrate

    migrate(database_path(), connect)
