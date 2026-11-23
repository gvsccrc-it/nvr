"""An MQTT or Home Assistant event source counts the messages it did not keep (#528).

MQTT messages had no allowance: every message on a matching topic, an event or not, became a stored event in its own write, kept 90 days.
As decided on the ticket, a source now keeps the same allowance as an HTTP source, and a message that is not an event and matches no mapping
is not stored. Both are counted here, from ``counted_since``, and shown in the source's status.
"""
from __future__ import annotations

import sqlite3

NAME = "event_source_drops"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE event_sources ADD COLUMN dropped_count INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE event_sources ADD COLUMN unmapped_count INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE event_sources ADD COLUMN counted_since TEXT")
