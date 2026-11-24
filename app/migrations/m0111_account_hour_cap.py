"""Wrong passwords for one account from outside the local network are capped per hour (#457, the owner's decision of 24 November 2026).

The per-address limits and the account-wide slowdown forget failures after a quiet spell (``app.auth_limits.FORGET_AFTER``), so a guesser
pacing tries across many addresses could keep going all day. ``auth_account_hours`` counts the wrong passwords that counted against an
account (not from the local network, not from a browser that has signed in to it before) in each clock hour; at the cap the account refuses
attempts from outside until the hour ends. The NVR keeps the current and previous hour only.
"""
from __future__ import annotations

import sqlite3

NAME = "account_hour_cap"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS auth_account_hours (account TEXT NOT NULL, hour INTEGER NOT NULL, failures INTEGER NOT NULL, "
               "PRIMARY KEY (account, hour))")
