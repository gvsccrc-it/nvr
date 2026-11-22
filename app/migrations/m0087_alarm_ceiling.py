"""A ceiling on the alarms one rule raises on one camera (#514).

``alarm_suppressions`` holds, for a rule and camera whose alarms are being held back, how many have been since the last summary alarm and
when that was. ``alarms_rule_camera_time_idx`` answers "how many alarms has this rule raised on this camera in the last minute and hour",
which is asked for every alarm raised.
"""
from __future__ import annotations

import sqlite3

NAME = "alarm_ceiling"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS alarm_suppressions (rule_id TEXT NOT NULL, camera_id TEXT NOT NULL, started_at TEXT NOT NULL, "
               "held_back INTEGER NOT NULL DEFAULT 0, last_summary_at TEXT NOT NULL, PRIMARY KEY (rule_id, camera_id))")
    db.execute("CREATE INDEX IF NOT EXISTS alarms_rule_camera_time_idx ON alarms(rule_id,camera_id,occurred_at)")
