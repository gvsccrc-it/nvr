"""Is the disk under a recordings folder the one this NVR recorded to? (#479)

When the host disk behind a recordings folder fails to mount, the folder is still there, empty, on whatever disk is underneath (usually
the small system disk). The NVR used to record into it, run that disk out of space, and then "remove" the oldest footage to make room:
no file was found, so each row was counted as already deleted, 5,000 rows a minute. When the real disk came back its footage had no rows
and could not be played, exported or ever deleted by the NVR.

So every storage volume carries a small marker file at its root, holding an id that the database also holds:

* **the marker matches**: the volume is the right disk;
* **no marker and no footage rows for the volume**: a first use; the marker is written;
* **no marker, but a file on the volume matches a footage row**: an installation from before markers (or a marker deleted by hand); the
  footage itself is the proof, and the marker is written;
* **no marker, footage rows, and none of their files**: the disk is not there. Nothing is recorded to the volume and nothing on it is
  deleted, by headroom, retention or tiering, until it is back;
* **another id**: a disk from another installation, treated the same way.

Replacing a disk on purpose is an administrator's explicit act (``adopt``), from Settings › Recording & storage.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .store import connect

logger = logging.getLogger("nvr.storage")

MARKER_NAME = ".nvr-volume.json"
CACHE_SECONDS = 5.0  # How long an answer is reused: playback asks once per segment
PROOF_SAMPLES = 25  # The newest and the oldest this many footage rows are looked for on the disk
PRESENT, UNMARKED, WRONG_DISK = "present", "unmarked", "wrong_disk"

_cache: dict[str, tuple[float, str]] = {}


def forget() -> None:
    _cache.clear()


def marker_file(path: Path) -> Path:
    return path / MARKER_NAME


def _on_disk(path: Path) -> str | None:
    try:
        found = json.loads(marker_file(path).read_text(encoding="utf-8"))
        return str(found["id"]) if isinstance(found, dict) and found.get("id") else None
    except (OSError, ValueError):
        return None


def _write(path: Path, volume_id: str, marker_id: str) -> bool:
    target = marker_file(path)
    partial = target.with_name(f"{MARKER_NAME}.{os.getpid()}.partial")
    try:
        partial.write_text(json.dumps({"id": marker_id, "volume": volume_id, "what": "Marks this folder as a recordings volume of one NVR. Do not delete or copy it."}), encoding="utf-8")
        os.replace(partial, target)
        return True
    except OSError:
        partial.unlink(missing_ok=True)
        return False


def _prefix_filter(volume_id: str, volumes: dict[str, str]) -> tuple[str, list[str]]:
    """The SQL that picks a volume's footage by path: under its own folder, and under no other volume's folder inside it."""
    def like(prefix: str) -> str:
        return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"

    own = volumes[volume_id].strip("/")
    clauses, values = [], []
    if own:
        clauses.append("relative_path LIKE ? ESCAPE '\\'")
        values.append(like(own))
    for other, relative in volumes.items():
        inner = relative.strip("/")
        if other != volume_id and inner and (not own or inner.startswith(own + "/")):
            clauses.append("relative_path NOT LIKE ? ESCAPE '\\'")
            values.append(like(inner))
    return " AND ".join(["tier_id IS NULL", "unavailable_at IS NULL", *clauses]), values


def _footage(db: sqlite3.Connection, volume_id: str, volumes: dict[str, str]) -> list[str]:
    """Some of the footage the database says is on this volume: the newest and the oldest."""
    where, values = _prefix_filter(volume_id, volumes)
    paths = []
    for order in ("DESC", "ASC"):
        paths += [row[0] for row in db.execute(f"SELECT relative_path FROM recording_segments WHERE {where} ORDER BY id {order} LIMIT {PROOF_SAMPLES}", values)]
    return paths


def _expected(db: sqlite3.Connection, volume_id: str) -> str | None:
    row = db.execute("SELECT marker_id FROM storage_volumes WHERE id=?", (volume_id,)).fetchone()
    return str(row[0]) if row and row[0] else None


def _mark(volume_id: str, path: Path, volumes: dict[str, str], *, replace: bool = False) -> None:
    """Agree an id with the database and write it to the volume. Two processes at a first start end with the same id: the database's."""
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO storage_volumes(id,relative_path) VALUES(?,?)", (volume_id, volumes[volume_id]))
        if replace:
            db.execute("UPDATE storage_volumes SET marker_id=? WHERE id=?", (uuid.uuid4().hex, volume_id))
        else:
            db.execute("UPDATE storage_volumes SET marker_id=? WHERE id=? AND marker_id IS NULL", (_on_disk(path) or uuid.uuid4().hex, volume_id))
        marker_id = _expected(db, volume_id)
    if marker_id and not _write(path, volume_id, marker_id):
        logger.warning("The marker file of recordings volume %s could not be written (%s)", volume_id, marker_file(path))


def _evaluate(volume_id: str, path: Path) -> str:
    from .recording import recordings_path
    from .storage import configured_volumes

    volumes = configured_volumes()
    on_disk = _on_disk(path) if path.is_dir() else None
    with connect() as db:
        expected = _expected(db, volume_id)
        if expected and on_disk == expected:
            return PRESENT
        footage = _footage(db, volume_id, volumes)
    if on_disk and expected:
        return WRONG_DISK
    root = recordings_path()
    proven = any((root / relative).is_file() for relative in footage)
    if footage and not proven:
        return WRONG_DISK if on_disk else UNMARKED
    if path.is_dir():
        _mark(volume_id, path, volumes)  # A first use, or footage that proves the disk
    return PRESENT


def state(volume_id: str, path: Path) -> str:
    key = f"{volume_id}\0{path}"
    found = _cache.get(key)
    now = time.monotonic()
    if found is not None and now - found[0] < CACHE_SECONDS:
        return found[1]
    try:
        result = _evaluate(volume_id, path)
    except (sqlite3.Error, ValueError):
        return found[1] if found is not None else PRESENT  # The database cannot say: this is not the place to stop recording
    _cache[key] = (now, result)
    return result


def verified(volume_id: str, path: Path) -> bool:
    return state(volume_id, path) == PRESENT


def explain(volume_state: str) -> str:
    if volume_state == WRONG_DISK:
        return "the disk mounted there belongs to another NVR (its marker file does not match). Mount the right disk"
    return ("the disk this NVR recorded to is not mounted there (its marker file is missing and none of the recorded files are there). Nothing is recorded to it "
            "and none of its footage is removed until it is back. Check that the disk is connected and mounted")


def adopt(volume_id: str, path: Path, *, clear_footage: bool) -> int:
    """The administrator says the disk now mounted is the volume from here on (a new, empty disk, or the disks of a restored installation).
    With ``clear_footage`` the rows of the footage that was on the old disk are removed; returns how many."""
    from .storage import configured_volumes

    volumes = configured_volumes()
    if not path.is_dir():
        raise OSError("The volume's folder is not there")
    removed = 0
    if clear_footage:
        where, values = _prefix_filter(volume_id, volumes)
        where = where.replace("unavailable_at IS NULL AND ", "").replace(" AND unavailable_at IS NULL", "")
        while True:
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    done = db.execute(f"DELETE FROM recording_segments WHERE id IN (SELECT id FROM recording_segments WHERE {where} LIMIT 500)", values).rowcount
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")
            removed += done
            if done < 500:
                break
    _mark(volume_id, path, volumes, replace=True)
    forget()
    return removed
