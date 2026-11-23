"""Face-watch embeddings and pictures are kept encrypted with the credential key (#520).

The numbers that describe a watched person's face, the picture they were enrolled from and each sighting's picture were plain data in the
database, so they were readable in any copy of the file. As decided on the ticket, they are now encrypted with the credential key, as camera
passwords are, and decrypted only where they are used: the AI worker's matching and the two picture routes.

Each column is renamed to end in ``_ciphertext``, the name the start-up check looks for when it decides whether a missing credential key
may be replaced: with face data stored, it may not, or the data would be lost without a word. What was stored is encrypted in place, with
``secure_delete`` on so that the plain bytes are overwritten in the file rather than left in freed pages. The pre-migration copy made just
before this step still holds them in the clear until it is rotated out (docs/face-watch.md says so).
"""
from __future__ import annotations

import sqlite3

NAME = "face_data_encrypted"

COLUMNS = (("face_embeddings", "embedding", "id"), ("face_watch_entries", "picture", "id"), ("face_sightings", "picture", "id"))


def upgrade(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA secure_delete = ON")
    for table, column, key in COLUMNS:
        db.execute(f"ALTER TABLE {table} RENAME COLUMN {column} TO {column}_ciphertext")
        rows = db.execute(f"SELECT {key},{column}_ciphertext FROM {table} WHERE {column}_ciphertext IS NOT NULL").fetchall()
        if not rows:
            continue
        from app.credentials import encrypt_bytes  # Only with data to encrypt: an installation without face data needs no key for this step

        db.executemany(f"UPDATE {table} SET {column}_ciphertext=? WHERE {key}=?", ((encrypt_bytes(row[1]), row[0]) for row in rows))


def verify(db: sqlite3.Connection) -> None:
    for table, column, _ in COLUMNS:
        if db.execute(f"SELECT 1 FROM {table} WHERE {column}_ciphertext IS NOT NULL AND CAST(substr({column}_ciphertext,1,5) AS TEXT)<>'gAAAA' LIMIT 1").fetchone():
            raise RuntimeError(f"{table}.{column} still holds a value that is not encrypted")
