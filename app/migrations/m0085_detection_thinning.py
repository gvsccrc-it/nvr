"""Thinning the detections of objects that are not moving (#512).

``ai_detections.stands_for``: how many analysed frames a row stands for, itself and the ones thinned after it (1 for every row there is).
The trigger that keeps a track's count right when a detection is deleted now takes off what the row stood for. ``detection_thinning``
holds how far thinning has got; it starts at the newest row there is, so nothing stored before this version is ever thinned: no large
table is rewritten on upgrade (adding a column with a default does not rewrite one either).
"""
from __future__ import annotations

import sqlite3

NAME = "detection_thinning"


def upgrade(db: sqlite3.Connection) -> None:
    if "stands_for" not in {row[1] for row in db.execute("PRAGMA table_info(ai_detections)")}:
        db.execute("ALTER TABLE ai_detections ADD COLUMN stands_for INTEGER NOT NULL DEFAULT 1")
    db.execute("CREATE TABLE IF NOT EXISTS detection_thinning (singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1), last_id INTEGER NOT NULL)")
    db.execute("INSERT OR IGNORE INTO detection_thinning(singleton_id,last_id) SELECT 1,COALESCE(MAX(id),0) FROM ai_detections")
    db.execute("DROP TRIGGER IF EXISTS ai_detections_track_removed")
    db.execute(
        """
        CREATE TRIGGER ai_detections_track_removed AFTER DELETE ON ai_detections BEGIN
            DELETE FROM ai_tracks WHERE id=OLD.track_id
                AND NOT EXISTS (SELECT 1 FROM ai_detections WHERE track_id=OLD.track_id);
            UPDATE ai_tracks SET
                first_seen_at=(SELECT min(detected_at) FROM ai_detections WHERE track_id=OLD.track_id),
                last_seen_at=(SELECT max(detected_at) FROM ai_detections WHERE track_id=OLD.track_id),
                detection_count=detection_count-OLD.stands_for
            WHERE id=OLD.track_id;
        END
        """
    )
