"""The NVR asks a camera that applies its own privacy masks whether it still holds them (#468).

A camera that accepted its masks used to be trusted for good: one factory-reset, updated, edited directly or replaced at the same address then
streamed unmasked. Now the masks are confirmed on the camera regularly, and the NVR draws them itself whenever they cannot be confirmed.

- ``privacy_masks.checked_at``: when the camera was last asked; ``confirmed_at``: when it last answered with the masks; ``unreachable_since``:
  when it first could not be asked, in the current run of failures; ``check_requested``: ask at the next pass (the stream reconnected, or the
  camera's source or login was saved); ``always_server``: an administrator's switch, "Always mask on the NVR as well", off by default;
  ``trust_version``: counts changes to whether the camera is trusted, so open live streams in another process notice.
- ``privacy_mask_gaps``: the spans in which the camera's masks were not confirmed. Footage recorded in one is masked by the NVR on the way
  out. A gap starts at the last confirmation (nobody knows when the masks went) and ends when they are confirmed again; an open one has no end.
"""
from __future__ import annotations

import sqlite3

NAME = "mask_confirmation"

COLUMNS = {
    "checked_at": "TEXT",
    "confirmed_at": "TEXT",
    "unreachable_since": "TEXT",
    "check_requested": "INTEGER NOT NULL DEFAULT 0 CHECK (check_requested IN (0,1))",
    "always_server": "INTEGER NOT NULL DEFAULT 0 CHECK (always_server IN (0,1))",
    "trust_version": "INTEGER NOT NULL DEFAULT 0",
}


def upgrade(db: sqlite3.Connection) -> None:
    present = {row[1] for row in db.execute("PRAGMA table_info(privacy_masks)")}
    for name, kind in COLUMNS.items():
        if name not in present:
            db.execute(f"ALTER TABLE privacy_masks ADD COLUMN {name} {kind}")
    # Masks the camera applied before this change were confirmed when they were written: that is where the first gap would start
    db.execute("UPDATE privacy_masks SET confirmed_at=camera_masked_since WHERE camera_side='applied' AND confirmed_at IS NULL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS privacy_mask_gaps ("
        "id INTEGER PRIMARY KEY, camera_id TEXT NOT NULL REFERENCES cameras(id) ON DELETE CASCADE, "
        "started_at TEXT NOT NULL, ended_at TEXT, reason TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS privacy_mask_gaps_camera_idx ON privacy_mask_gaps(camera_id, started_at)")
    # Read by every pass and every viewer: which cameras' masks are due to be asked about
    db.execute("CREATE INDEX IF NOT EXISTS privacy_masks_applied_idx ON privacy_masks(camera_side, checked_at)")
