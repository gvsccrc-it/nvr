"""The failover link between the two NVRs is HTTPS by default, each pinning the other's certificate (#502, part 2).

``peer_fingerprint`` is the SHA-256 of the other NVR's link certificate, entered when the two are paired. ``allow_unencrypted`` is
"Allow an unencrypted link": plain HTTP between the two works only while it is ticked on both. ``legacy_link`` keeps a pair that was set
up before this release working as it was (plain HTTP, or requests through the web port) until its address is changed or failover is
switched off; the hardening page lists it with the way to move it to HTTPS.
"""
from __future__ import annotations

import sqlite3

NAME = "failover_link"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE failover_settings ADD COLUMN peer_fingerprint TEXT")
    db.execute("ALTER TABLE failover_settings ADD COLUMN allow_unencrypted INTEGER NOT NULL DEFAULT 0 CHECK(allow_unencrypted IN (0,1))")
    db.execute("ALTER TABLE failover_settings ADD COLUMN legacy_link INTEGER NOT NULL DEFAULT 0 CHECK(legacy_link IN (0,1))")
    # Every pair configured before the upgrade keeps working as it did: the owner's decision (24 November 2026)
    db.execute("UPDATE failover_settings SET legacy_link=1 WHERE role<>'off' AND peer_url IS NOT NULL")
