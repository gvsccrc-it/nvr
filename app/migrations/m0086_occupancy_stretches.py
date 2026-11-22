"""When an occupancy alarm's over-limit stretch ended (#513).

An occupancy rule now raises one alarm for one stretch, and its event records when the count dropped back (``ended_at``). The rule's
running count uses it after a restart: an event with no end is a stretch that may still be going. Events from before this are given
their own time as their end, so none of them is taken for a stretch still open.
"""
from __future__ import annotations

import sqlite3

NAME = "occupancy_stretches"


def upgrade(db: sqlite3.Connection) -> None:
    if "ended_at" not in {row[1] for row in db.execute("PRAGMA table_info(rule_events)")}:
        db.execute("ALTER TABLE rule_events ADD COLUMN ended_at TEXT")
    db.execute("UPDATE rule_events SET ended_at=occurred_at WHERE condition_type='occupancy' AND ended_at IS NULL")
