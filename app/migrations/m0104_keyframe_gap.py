"""Camera health says when a camera's keyframes are too far apart for instant live view (#496).

A camera in a "smart codec" mode can go a minute between keyframes. The live view keeps what came since the latest one, within bounds, so a
viewer who joins starts at once; past those bounds a viewer waits for the next keyframe. The live hub records the interval it saw here when
it is that long, and clears it when it is not.
"""
from __future__ import annotations

import sqlite3

NAME = "keyframe_gap"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE camera_health ADD COLUMN keyframe_gap_seconds REAL")
    db.execute("ALTER TABLE camera_health ADD COLUMN keyframe_gap_at TEXT")
