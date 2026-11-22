"""Each passkey remembers the address it was registered at (#464).

Without ``NVR_PUBLIC_URL``, a passkey answer was checked against the address named by the request's own ``Origin`` header, which the
caller writes: the comparison always passed, and only the authenticator's own tie to the host name was left (not the port). The site id
and origin a passkey was registered with are now stored and checked. Passkeys that already exist have neither; each is filled in the
first time it is used successfully.
"""
from __future__ import annotations

import sqlite3

NAME = "passkey_origin"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE user_passkeys ADD COLUMN rp_id TEXT")
    db.execute("ALTER TABLE user_passkeys ADD COLUMN origin TEXT")
