"""The retention forecast, kept once worked out (#460).

Working out the forecast adds up every recorded segment of every camera. It was done afresh for each metrics scrape (every 15 to 60
seconds, by design) and each forecast page, about half a second at a million segments and growing with the footage kept. The recorder
works it out every five minutes anyway; the answer is now kept here and the readers use it.
"""
from __future__ import annotations

import sqlite3

NAME = "forecast_snapshot"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE forecast_snapshot (id INTEGER PRIMARY KEY CHECK (id=1), computed_at TEXT NOT NULL, result_json TEXT NOT NULL)")
