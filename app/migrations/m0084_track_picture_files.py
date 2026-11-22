"""An object's first picture is a file, not a blob in the database (#512).

``track_keyframes`` gains where the file is, its size and its camera; the picture itself is no longer stored in ``jpeg`` for new rows
(the column stays, empty, because it is NOT NULL and the table is not rewritten: existing rows keep their blob until they age out).

Two small tables are kept by triggers: the files to delete once their rows have gone, and each camera's count and bytes of pictures.
"""
from __future__ import annotations

import sqlite3

NAME = "track_picture_files"

_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS track_picture_removals (relative_path TEXT PRIMARY KEY, removed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))",
    "CREATE TABLE IF NOT EXISTS track_picture_totals (camera_id TEXT PRIMARY KEY, files INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0)",
    """
    CREATE TRIGGER IF NOT EXISTS track_keyframes_file_added AFTER INSERT ON track_keyframes WHEN NEW.relative_path IS NOT NULL BEGIN
        INSERT INTO track_picture_totals(camera_id,files,bytes) VALUES(COALESCE(NEW.camera_id,''),1,NEW.size_bytes)
            ON CONFLICT(camera_id) DO UPDATE SET files=files+1,bytes=bytes+excluded.bytes;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS track_keyframes_file_removed AFTER DELETE ON track_keyframes WHEN OLD.relative_path IS NOT NULL BEGIN
        INSERT OR IGNORE INTO track_picture_removals(relative_path) VALUES(OLD.relative_path);
        UPDATE track_picture_totals SET files=MAX(0,files-1),bytes=MAX(0,bytes-OLD.size_bytes) WHERE camera_id=COALESCE(OLD.camera_id,'');
    END
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    have = {row[1] for row in db.execute("PRAGMA table_info(track_keyframes)")}
    for column, kind in (("relative_path", "TEXT"), ("size_bytes", "INTEGER NOT NULL DEFAULT 0"), ("camera_id", "TEXT")):
        if column not in have:
            db.execute(f"ALTER TABLE track_keyframes ADD COLUMN {column} {kind}")  # Adding a column does not rewrite the table
    for statement in _STATEMENTS:
        db.execute(statement)
