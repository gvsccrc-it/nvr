"""How big the database is, and which tables make it so (#512).

A large database file is what makes backups, the copy before an upgrade and a standby's sync slow, and the data disk filling was part of a
production incident (#455). Its size is one ``stat``. Which tables hold it takes reading every page (SQLite's ``dbstat``), minutes on a
large file, so that is measured once a night, straight after the nightly integrity check (which has just read every page), and kept in a
small file beside the database for the status page and ``/metrics`` to read.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import connect, database_path, log_bytes

logger = logging.getLogger(__name__)
TABLES_KEPT = 12  # The largest tables; the rest are added up as "other"


def _report_path() -> Path:
    path = database_path()
    return path.with_name(f"{path.name}.table-sizes.json")


def measure() -> dict[str, Any] | None:
    """Read how many bytes each table holds, its indexes included, and keep the result. None when this SQLite cannot say (no ``dbstat``)."""
    started = time.monotonic()
    try:
        with connect() as db:
            rows = db.execute(
                "SELECT s.tbl_name AS name,SUM(d.pgsize) AS bytes FROM dbstat d JOIN sqlite_schema s ON s.name=d.name GROUP BY s.tbl_name ORDER BY bytes DESC").fetchall()
            free = int(db.execute("PRAGMA freelist_count").fetchone()[0]) * int(db.execute("PRAGMA page_size").fetchone()[0])
    except sqlite3.OperationalError as error:
        logger.info("The sizes of the database's tables were not measured: %s", error)
        return None
    tables = [{"name": row["name"], "bytes": int(row["bytes"])} for row in rows]
    report = {"measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), "seconds": round(time.monotonic() - started, 1),
              "tables": tables[:TABLES_KEPT], "other_bytes": sum(table["bytes"] for table in tables[TABLES_KEPT:]), "free_bytes": free}
    path = _report_path()
    partial = path.with_name(f"{path.name}.part")
    try:
        partial.write_text(json.dumps(report), encoding="utf-8")
        os.replace(partial, path)
    except OSError as error:
        logger.warning("The sizes of the database's tables could not be kept: %s", error)
    largest = ", ".join(f"{table['name']} {table['bytes'] / (1 << 20):.1f} MB" for table in tables[:5])
    logger.info("The database's largest tables: %s (measured in %.1f s)", largest or "none", report["seconds"])
    return report


def report() -> dict[str, Any]:
    """The database's size now, and its largest tables as last measured (``tables`` is None until the first nightly check)."""
    try:
        size = database_path().stat().st_size
    except OSError:
        size = 0
    result: dict[str, Any] = {"bytes": size, "log_bytes": log_bytes(), "tables": None, "measured_at": None, "other_bytes": 0, "free_bytes": 0}
    try:
        kept = json.loads(_report_path().read_text(encoding="utf-8"))
        tables = [{"name": str(table["name"]), "bytes": int(table["bytes"])} for table in kept["tables"]]
        result.update(tables=tables, measured_at=str(kept["measured_at"]), other_bytes=int(kept.get("other_bytes", 0)), free_bytes=int(kept.get("free_bytes", 0)))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result
