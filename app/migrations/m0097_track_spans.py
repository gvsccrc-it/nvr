"""Each camera's longest track, so a search for a time range reads only the tracks that can reach it (#515).

A track is in range when it began before the range's end and ended after its start. No index can answer that narrowly: the search read every
later track (8 s at 10 million), and a rule's test every track of every camera. Knowing how long a camera's longest track lasted, a track that
reaches the range began at most that long before its start, so the search reads the tracks that began from then to the range's end, through the
(camera, first seen) index. The longest only grows: a shortened or removed track leaves it as it was, which reads a little more, never less.
"""
from __future__ import annotations

import sqlite3

NAME = "track_spans"

# How long a track lasted, in seconds; a time SQLite cannot read counts as endless, so the search reads everything rather than miss the track
SPAN = "COALESCE((julianday({t}.last_seen_at)-julianday({t}.first_seen_at))*86400.0, 1e12)"
KEEP = ("INSERT INTO ai_track_spans(camera_id,longest_seconds) VALUES(NEW.camera_id,{span}) "
        "ON CONFLICT(camera_id) DO UPDATE SET longest_seconds=excluded.longest_seconds WHERE excluded.longest_seconds>ai_track_spans.longest_seconds;")
LONGER = "{span} > COALESCE((SELECT longest_seconds FROM ai_track_spans WHERE camera_id=NEW.camera_id), -1)"
# Worked out again from the tracks: here, and after a salvage (app.db_salvage)
RESPAN = ("DELETE FROM ai_track_spans",
          f"INSERT INTO ai_track_spans(camera_id,longest_seconds) SELECT camera_id,MAX({SPAN.format(t='ai_tracks')}) FROM ai_tracks GROUP BY camera_id")


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS ai_track_spans (camera_id TEXT PRIMARY KEY, longest_seconds REAL NOT NULL)")
    for statement in RESPAN:
        db.execute(statement)
    span = SPAN.format(t="NEW")
    # Written only when a track grows past the camera's longest: a read of one row on each extension, a write rarely
    db.execute(f"CREATE TRIGGER IF NOT EXISTS ai_track_spans_insert AFTER INSERT ON ai_tracks WHEN {LONGER.format(span=span)} BEGIN {KEEP.format(span=span)} END")
    db.execute(f"CREATE TRIGGER IF NOT EXISTS ai_track_spans_update AFTER UPDATE OF first_seen_at,last_seen_at,camera_id ON ai_tracks "
               f"WHEN {LONGER.format(span=span)} BEGIN {KEEP.format(span=span)} END")
