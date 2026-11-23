"""A camera's signed footage chain that no longer matches its signature is recorded as a break, and signing carries on from it (#507).

Each minute the chain's new links are checked from the last signature before the head is signed. A link that had changed was only logged, every
minute for good, and nothing recorded on that camera was signed again. As decided on the ticket, the break is now recorded, signed with the
installation's key, and signing resumes at once from the head it names. The record is permanent: it can be neither deleted nor changed, except
that an administrator acknowledges it once, with a note; a second break is a record of its own.
"""
from __future__ import annotations

import sqlite3

NAME = "footage_chain_breaks"

_STATEMENTS = (
    """
    CREATE TABLE footage_chain_breaks (
        id TEXT PRIMARY KEY,
        camera_id TEXT NOT NULL,
        broken_seq INTEGER NOT NULL,
        signed_seq INTEGER NOT NULL,
        resumed_seq INTEGER NOT NULL,
        head_hash TEXT NOT NULL,
        reason TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        signature TEXT NOT NULL,
        public_key TEXT NOT NULL,
        acknowledged_by INTEGER,
        acknowledged_at TEXT,
        acknowledgement_note TEXT
    )
    """,
    "CREATE INDEX footage_chain_breaks_camera ON footage_chain_breaks(camera_id, resumed_seq)",
    """
    CREATE TRIGGER footage_chain_breaks_kept BEFORE DELETE ON footage_chain_breaks
    BEGIN
        SELECT RAISE(ABORT, 'A footage chain break is a permanent record');
    END
    """,
    """
    CREATE TRIGGER footage_chain_breaks_fixed BEFORE UPDATE ON footage_chain_breaks
    WHEN OLD.acknowledged_at IS NOT NULL OR NEW.id IS NOT OLD.id OR NEW.camera_id IS NOT OLD.camera_id OR NEW.broken_seq IS NOT OLD.broken_seq
      OR NEW.signed_seq IS NOT OLD.signed_seq OR NEW.resumed_seq IS NOT OLD.resumed_seq OR NEW.head_hash IS NOT OLD.head_hash
      OR NEW.reason IS NOT OLD.reason OR NEW.detected_at IS NOT OLD.detected_at OR NEW.signature IS NOT OLD.signature
      OR NEW.public_key IS NOT OLD.public_key OR NEW.acknowledged_at IS NULL OR NEW.acknowledgement_note IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'A footage chain break is a permanent record: only its acknowledgement is added, once');
    END
    """,
)


def upgrade(db: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        db.execute(statement)
