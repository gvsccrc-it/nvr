"""The hardening checks say how far a run has got, and which cameras did not answer (#477).

"Run checks" used to probe every camera inside the request: a camera that does not answer costs up to 15 s, four at a time, so fifty
offline cameras held the request for about three minutes (a reverse proxy gives up long before). A run now goes on in the background and
records its progress here, for the page to show.
"""
from __future__ import annotations

import sqlite3

NAME = "hardening_run_progress"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE hardening_settings ADD COLUMN run_started_at TEXT")
    db.execute("ALTER TABLE hardening_settings ADD COLUMN run_probes_total INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE hardening_settings ADD COLUMN run_probes_done INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE hardening_settings ADD COLUMN run_not_answered_json TEXT NOT NULL DEFAULT '[]'")
