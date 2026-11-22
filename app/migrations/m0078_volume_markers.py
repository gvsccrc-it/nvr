"""Each storage volume's marker id (#479).

A recordings volume carries a small file holding this id, so that an empty folder left by a disk that failed to mount is not taken for the
volume. The id is agreed the first time the volume is seen after this upgrade: see ``app/volume_marker.py``.
"""
from __future__ import annotations

import sqlite3

NAME = "volume_markers"


def upgrade(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute("PRAGMA table_info(storage_volumes)")}
    if "marker_id" not in columns:
        db.execute("ALTER TABLE storage_volumes ADD COLUMN marker_id TEXT")
