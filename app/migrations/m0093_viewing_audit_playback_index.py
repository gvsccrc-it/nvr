"""Indexes for the two viewing-audit questions asked most often (#461).

Every recorded segment someone plays asks for their open playback session on that camera. The only index that helped was (person, time
started), so the question read every row that person (or the administrator behind an API token or video wall) ever produced: 9.5 ms at
100,000 rows, 46 ms at 400,000, many times a second during a sixteen-camera playback. The first index here is bounded by person, camera
and time ended.

The second serves the five-minute clean-up's "is anything old enough to remove?", which filters on when an entry ended (or was last seen)
and could use no index, so it read the whole table even when nothing was due.
"""
from __future__ import annotations

import sqlite3

NAME = "viewing_audit_playback_index"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS viewing_audit_playback_idx ON viewing_audit(user_id, camera_id, ended_at) WHERE kind='playback'")
    db.execute("CREATE INDEX IF NOT EXISTS viewing_audit_finished_idx ON viewing_audit(COALESCE(ended_at, last_seen_at))")
