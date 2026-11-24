"""A shared live view can keep streaming while its page is hidden (#533).

A Live page hidden for two minutes now gives its streams back. A control-room screen that must keep streaming while hidden uses a shared
view an administrator has marked so: ``shared_viewer_layouts.keep_streaming``, off by default, set per view and not per person.
"""
from __future__ import annotations

import sqlite3

NAME = "keep_streaming"


def upgrade(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(shared_viewer_layouts)")}
    if "keep_streaming" not in columns:
        db.execute("ALTER TABLE shared_viewer_layouts ADD COLUMN keep_streaming INTEGER NOT NULL DEFAULT 0 CHECK (keep_streaming IN (0,1))")
