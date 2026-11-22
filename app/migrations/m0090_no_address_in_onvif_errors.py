"""Stored error text no longer holds a camera's address (#471, after #377).

Camera network addresses are for administrators only. The message for an ONVIF service that does not answer named the host, and that
text is kept and shown to operators and read-only tokens: a camera's last error, its motion capability's message, a backfill job's
message, and incidents made from it. New messages no longer name the host; this takes it out of the ones already stored.
"""
from __future__ import annotations

import re
import sqlite3

NAME = "no_address_in_onvif_errors"

# The two forms the message took, the host being whatever stood between the fixed words
_FOR_HOST = re.compile(r"(No ONVIF service answered at the device service address) for \S+?(: check the address)")
_AT_HOST = re.compile(r"(No ONVIF service answered) at \S+ (on port )")
_COLUMNS = (("cameras", "id", "last_error"), ("cameras", "id", "backfill_support_message"), ("camera_capabilities", "rowid", "message"),
            ("backfill_jobs", "rowid", "message"), ("incidents", "id", "summary"), ("incidents", "id", "details_json"))


def without_host(text: str) -> str:
    return _AT_HOST.sub(r"\1 \2", _FOR_HOST.sub(r"\1\2", text))


def upgrade(db: sqlite3.Connection) -> None:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, key, column in _COLUMNS:
        if table not in tables or column not in {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}:
            continue
        for row in db.execute(f'SELECT {key},"{column}" FROM "{table}" WHERE "{column}" LIKE \'%No ONVIF service answered%\'').fetchall():
            cleaned = without_host(row[1])
            if cleaned != row[1]:
                db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE {key}=?', (cleaned, row[0]))
