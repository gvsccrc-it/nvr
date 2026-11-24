"""Whether a camera may be at a public address (#474).

The NVR connects to a camera only at an address the destination rule allows (``app/camera_destination.py``). A public address needs the
camera's own permission, ``cameras.public_address``:

- ``allowed``: an administrator signed in with a browser ticked "This camera is at a public address" when saving its source (security log:
  ``camera_public_address_allowed``);
- ``grandfathered``: the camera was there before the rule, so it keeps working after the upgrade. It is listed on the hardening page when
  its address is public, and the next change to its source needs the tick;
- NULL: not allowed (every camera added after the upgrade, until ticked).

Every camera that exists at the upgrade is ``grandfathered``: which of them are at a public address is only known by looking their address
up (and a direct stream address is encrypted), and none of them may stop recording because of the upgrade.
"""
from __future__ import annotations

import sqlite3

NAME = "camera_public_address"


def upgrade(db: sqlite3.Connection) -> None:
    present = {row[1] for row in db.execute("PRAGMA table_info(cameras)")}
    if "public_address" not in present:
        db.execute("ALTER TABLE cameras ADD COLUMN public_address TEXT CHECK (public_address IN ('allowed','grandfathered'))")
        db.execute("UPDATE cameras SET public_address='grandfathered'")
