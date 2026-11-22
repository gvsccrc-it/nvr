"""Which build ran before an upgrade (#540).

The rollback steps named the previous image by its version, a tag that exists only for a released version: an installation following the
latest build of main had no way back that could be typed. The image now knows the commit it was built from, each start remembers it, and
the history of upgrades keeps the build it came from and the build it went to.
"""
from __future__ import annotations

import sqlite3

NAME = "update_builds"


def upgrade(db: sqlite3.Connection) -> None:
    for table, column in (("update_settings", "seen_build"), ("update_history", "from_build"), ("update_history", "to_build")):
        if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
