"""An export's bulky manifest is dropped 30 days after its package expired; the record of the export is kept for good (#505).

As decided on the ticket: who asked, when, which cameras and range, and the SHA-256 of the package issued are what let the NVR say, years
later, whether a file is the package it issued, and they are small. The manifest, which held a cue for every second a grid tile had no
picture (megabytes for a long grid), is carried by the package itself. ``manifest_dropped_at`` says when it was dropped, so the pass that
drops them finds the rest without reading each manifest.
"""
from __future__ import annotations

import sqlite3

NAME = "export_manifest_dropped"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE exports ADD COLUMN manifest_dropped_at TEXT")
