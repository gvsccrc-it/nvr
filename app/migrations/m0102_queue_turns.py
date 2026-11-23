"""When an export started rendering and a redaction review started and finished analysing (#504).

The export queue and the redaction queue now say roughly how long a refused request would wait, which needs to know how long jobs take
here: the time from start to end, not from being asked for (which includes the wait in the queue). The start also bounds a whole export to
its time limit.
"""
from __future__ import annotations

import sqlite3

NAME = "queue_turns"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE exports ADD COLUMN started_at TEXT")
    db.execute("ALTER TABLE redaction_reviews ADD COLUMN started_at TEXT")
    db.execute("ALTER TABLE redaction_reviews ADD COLUMN analysed_at TEXT")
