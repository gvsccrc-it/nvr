"""A reminder before a maintenance window ends (#234, the owner's decision of 24 November 2026).

``maintenance_windows.remind_at`` is when the person who opened a window is told it is about to end, so they can extend it if the work is
not finished (``app.maintenance.reminder_time``); NULL when there is to be no reminder (a window of a few minutes, or one whose opener has
been removed). ``reminded_at`` is when the reminder went out; extending the window clears it, so the new end is announced in its turn.
The partial index answers the background worker's "is a reminder due?" without reading the windows that are done with.

``push_deliveries.maintenance_window_id`` names the window a push notification is about, for a reminder that is not about an alarm
(``alarm_id`` is then NULL).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

NAME = "maintenance_reminders"


def _reminder_time(starts_at: str, ends_at: str) -> str | None:
    """``app.maintenance.reminder_time`` as it was when this step was written: ten minutes before the end, or a quarter of the window's
    length before it for a window shorter than 40 minutes, and none for a window shorter than four minutes. Kept here so the step never changes."""
    start, end = (datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc) for value in (starts_at, ends_at))
    lead = min(timedelta(minutes=10), (end - start) / 4)
    return None if lead < timedelta(minutes=1) else (end - lead).isoformat(timespec="seconds")


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def upgrade(db: sqlite3.Connection) -> None:
    for column in ("remind_at", "reminded_at"):
        if column not in _columns(db, "maintenance_windows"):
            db.execute(f"ALTER TABLE maintenance_windows ADD COLUMN {column} TEXT")
    if "maintenance_window_id" not in _columns(db, "push_deliveries"):
        db.execute("ALTER TABLE push_deliveries ADD COLUMN maintenance_window_id TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS maintenance_windows_remind_idx ON maintenance_windows(remind_at) WHERE reminded_at IS NULL AND ended_at IS NULL")
    # Windows still open when this version starts get their reminder too; one already inside its reminder time is told on the next pass.
    for row in db.execute("SELECT id,starts_at,ends_at,created_by FROM maintenance_windows WHERE ended_at IS NULL AND remind_at IS NULL").fetchall():
        if row[3] is None:
            continue
        db.execute("UPDATE maintenance_windows SET remind_at=? WHERE id=?", (_reminder_time(row[1], row[2]), row[0]))


def verify(db: sqlite3.Connection) -> None:
    missing = {"remind_at", "reminded_at"} - _columns(db, "maintenance_windows")
    if missing or "maintenance_window_id" not in _columns(db, "push_deliveries"):
        raise RuntimeError("The maintenance reminder columns were not added")
