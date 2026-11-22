"""A camera may be allowed to ask for its password unprotected during two-way audio (#476).

Two-way audio used to answer any sign-in request the camera made: a request for Basic sign-in, over plain RTSP, was handed the camera's
user name and password merely encoded. By the owner's decision it no longer does, unless an administrator allows it for that one camera
(an older camera that can do nothing else). Off for every camera, existing ones included.
"""
from __future__ import annotations

import sqlite3

NAME = "talk_unprotected_sign_in"


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE camera_talk ADD COLUMN allow_basic INTEGER NOT NULL DEFAULT 0")
