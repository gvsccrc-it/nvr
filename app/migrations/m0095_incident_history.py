"""Incident history is bounded, and the incident list is cheap to poll (#516).

Nothing ever removed an incident or its events, and every open tab asked for every event of every incident every 15 seconds. An
incident now keeps a count of its events, so the list need not count them, and how many old events were removed from it, so its
history can say so. The indexes serve the list (newest first, all states or one camera's) and the history pass.
"""
from __future__ import annotations

import sqlite3

NAME = "incident_history"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE incidents ADD COLUMN event_count INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE incidents ADD COLUMN events_removed INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE incidents ADD COLUMN events_removed_before TEXT")
    db.execute("UPDATE incidents SET event_count=(SELECT COUNT(*) FROM incident_events e WHERE e.incident_id=incidents.id)")
    db.execute("CREATE INDEX IF NOT EXISTS incidents_seen_idx ON incidents(last_seen_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS incidents_camera_seen_idx ON incidents(camera_id,last_seen_at DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS incidents_recovered_idx ON incidents(recovered_at) WHERE state='recovered'")
    db.execute("CREATE INDEX IF NOT EXISTS incident_events_occurred_idx ON incident_events(occurred_at)")
