"""Indexes for the rows SQLite must find when their parent is deleted (#524), and for pruning the push queue (#525).

Deleting a row makes SQLite look for the rows that refer to it (to delete them, or to clear the reference). Without an index that starts
with the referring column, that is a read of the whole child table for every parent deleted: removing 500 old alarms with a million push
deliveries held the write lock for about a minute, and every other writer gave up after ten seconds.
"""
from __future__ import annotations

import sqlite3

NAME = "cascade_indexes"

INDEXES = (
    ("push_deliveries_alarm_idx", "push_deliveries(alarm_id)"),
    ("alarm_comments_alarm_idx", "alarm_comments(alarm_id)"),
    ("face_sightings_track_idx", "face_sightings(track_id)"),
    ("digest_deliveries_digest_idx", "digest_deliveries(digest_id)"),
    ("exports_retry_of_idx", "exports(retry_of)"),
    # The hourly prune of finished deliveries reads them by state and age (#525).
    ("push_deliveries_finished_idx", "push_deliveries(status, created_at)"),
)


def upgrade(db: sqlite3.Connection) -> None:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name, target in INDEXES:
        if target.split("(")[0] in tables:
            db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
