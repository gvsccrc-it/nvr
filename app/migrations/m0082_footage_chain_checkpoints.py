"""Where each camera's kept footage chain begins (#500).

The chain's links older than the kept period are replaced by a signed checkpoint: the link a kept signature is over, from which the chain
that remains still verifies. This table remembers that point for each camera, and from what time on footage is covered by the kept chain,
so that verifying older footage can say "before the kept chain" and not "tampered".
"""
from __future__ import annotations

import sqlite3

NAME = "footage_chain_checkpoints"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS footage_chain_checkpoints ("
        "camera_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, link_hash TEXT NOT NULL, kept_from TEXT NOT NULL, links_removed INTEGER NOT NULL DEFAULT 0, "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))")
