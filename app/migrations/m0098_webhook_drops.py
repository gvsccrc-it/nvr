"""A webhook receiver's dropped events are counted on the receiver, not stored one row each (#526).

Once 1,000 events were waiting for a receiver, each further event was still stored, payload and all, as a failed delivery: with object
detections subscribed at a busy site, over a million rows a day, kept thirty days. As decided on the ticket, each receiver now keeps how many
it dropped and when the first and the latest drop happened, shown beside it with a button to clear them.
"""
from __future__ import annotations

import sqlite3

NAME = "webhook_drops"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE webhook_endpoints ADD COLUMN dropped_count INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE webhook_endpoints ADD COLUMN dropped_first_at TEXT")
    db.execute("ALTER TABLE webhook_endpoints ADD COLUMN dropped_last_at TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS webhook_deliveries_waiting_idx ON webhook_deliveries(created_at) WHERE status='queued'")
