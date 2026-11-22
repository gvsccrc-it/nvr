"""The coverage record: an index for each camera's open interval (#492).

The recorder asks for a camera's open interval every five seconds. With only the (camera, start) index, every interval the camera had ever
had was read and sorted to find it: 51 ms at 200,000 intervals.
"""
from __future__ import annotations

import sqlite3

NAME = "coverage_indexes"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS coverage_open_idx ON coverage_intervals(camera_id, id) WHERE ends_at IS NULL")
