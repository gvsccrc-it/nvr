"""Tiering keeps up with the cameras on a volume, and says when it does not (#486).

- ``recording_segments_tier_due_idx`` lets a pass find the oldest footage still on the recorder's disks without reading and sorting the
  whole table (232 ms on a million rows, about 2 s at 8.6 million, every pass).
- ``cold_tiers.read_back``: the administrator's choice to read every file back from an object store after it is uploaded, instead of
  trusting the store's own SHA-256 checksum. ``cold_tiers.checksums`` remembers whether the store keeps one ('yes'/'no', unknown until a
  move finds out): a store that does not is read back in full.
- ``tier_backlog``: how much footage was waiting to move, sampled every few minutes per volume, so a backlog that keeps growing raises an
  incident instead of the footage being deleted by headroom with no warning.
"""
from __future__ import annotations

import sqlite3

NAME = "tiering_throughput"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS recording_segments_tier_due_idx ON recording_segments(ends_at) WHERE tier_id IS NULL AND unavailable_at IS NULL")
    columns = {row[1] for row in db.execute("PRAGMA table_info(cold_tiers)")}
    if "read_back" not in columns:
        db.execute("ALTER TABLE cold_tiers ADD COLUMN read_back INTEGER NOT NULL DEFAULT 0 CHECK (read_back IN (0,1))")
    if "checksums" not in columns:
        db.execute("ALTER TABLE cold_tiers ADD COLUMN checksums TEXT CHECK (checksums IN ('yes','no'))")
    db.execute("""
        CREATE TABLE IF NOT EXISTS tier_backlog (
            volume_id TEXT PRIMARY KEY,
            segments INTEGER NOT NULL,
            oldest_ends_at TEXT,
            samples_json TEXT NOT NULL DEFAULT '[]',
            measured_at TEXT NOT NULL
        )
    """)
