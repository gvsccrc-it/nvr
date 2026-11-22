"""Holds placed by operators get an expiry (#491, the owner's decision).

A hold placed by an operator now expires after 30 days. The ones that already exist are not cut short: each gets 30 days from this
upgrade. One audit row (``operator_holds_given_expiry``, in the security log) lists them for the administrator, who can extend any of
them or make it indefinite. Holds that belong to a case, and holds an administrator placed, are left as they are.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

NAME = "operator_hold_expiry"
DAYS = 30


def upgrade(db: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=DAYS)).isoformat()
    holds = db.execute(
        "SELECT h.id,h.segment_id,h.camera_id,u.username FROM recording_holds h JOIN users u ON u.id=h.created_by "
        "WHERE u.role!='administrator' AND h.case_id IS NULL AND h.released_at IS NULL AND h.expires_at IS NULL").fetchall()
    if not holds:
        return
    db.executemany("UPDATE recording_holds SET expires_at=? WHERE id=?", [(expires, hold[0]) for hold in holds])
    by_person: dict[str, int] = {}
    for hold in holds:
        by_person[hold[3]] = by_person.get(hold[3], 0) + 1
    details = {"holds": len(holds), "expires_at": expires, "by": by_person, "segments": [hold[1] for hold in holds][:500],
               "note": "Holds placed by operators now expire after 30 days. These existing ones run for 30 days from this upgrade; an administrator can extend any of them or make it indefinite."}
    db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(NULL,'operator_holds_given_expiry','recording_holds','upgrade',?)",
               (json.dumps(details, sort_keys=True),))
