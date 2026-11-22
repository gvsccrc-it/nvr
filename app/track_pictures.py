"""Each object's first picture, kept as a file on the recordings volume and not in the database (#512).

Every track stored the frame it was first analysed in as a 15 to 21 KB blob in ``track_keyframes``: 50 cameras with 3,000 objects a day
and 30 days kept is 4.5 million pictures, about 70 GB in the one database file, carried by every backup, pre-migration copy and
standby sync. The picture is now a file in its camera's folder::

    <camera's folder>/pictures/<YYYYMMDD>/<track id>.jpg

and ``track_keyframes`` keeps only where it is (``relative_path``, from the recordings root, as a segment's is) and how big.

* **Removed with the camera's retention.** A track goes when its last detection goes (a trigger), its ``track_keyframes`` row goes with
  it, and a trigger on that row notes the file in ``track_picture_removals``. ``sweep`` (the recorder's retention pass) deletes the noted
  files. A file whose volume is not mounted stays noted until it is: "no file found" on a disk that is not there is not "deleted" (#338).
* **Counted.** ``track_picture_totals`` holds each camera's number of pictures and their bytes, kept by triggers, so the figure costs
  nothing to read.
* **Never served as it is.** Nothing hands out the file: the two readers (a track's thumbnail and an alarm's picture) read it through
  ``read`` and draw the camera's privacy masks over it, exactly as they did with the blob.
* **Old rows are left alone.** A row written before this still holds its picture in ``jpeg`` and is read from there until it ages out.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import connect, has_row

logger = logging.getLogger(__name__)
FOLDER = "pictures"
DAY_NAME = re.compile(r"^\d{8}$")
SWEEP_BATCH = 500  # Files deleted in one step
MAX_BYTES = 2_000_000  # A picture larger than this is not one of ours


def _relative(camera_id: str, track_id: str, at: datetime) -> tuple[Path, str]:
    """Where a track's picture goes: the file, and its path from the recordings root."""
    from .recording import recordings_path
    from .storage import camera_recordings_dir

    if not re.fullmatch(r"[0-9a-fA-F-]{1,64}", track_id):
        raise ValueError("Not a track id")
    path = camera_recordings_dir(camera_id) / FOLDER / at.astimezone(timezone.utc).strftime("%Y%m%d") / f"{track_id}.jpg"
    return path, path.relative_to(recordings_path()).as_posix()


def write_file(camera_id: str, track_id: str, at: datetime, jpeg: bytes) -> str | None:
    """Write the picture's file (whole or not at all) and return its path from the recordings root; None when the camera's volume cannot
    be written to. No database connection is held: this is a disk write, and it runs before the row that names the file is stored."""
    try:
        path, relative = _relative(camera_id, track_id, at)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{path.name}.part")
        try:
            partial.write_bytes(jpeg)
            os.replace(partial, path)
        finally:
            partial.unlink(missing_ok=True)
        return relative
    except (OSError, ValueError) as error:
        logger.warning("The first picture of an object on camera %s was not kept: %s", camera_id, error)
        return None


SETTLE_SECONDS = 60  # A file noted for removal is left this long: one being written now is noted first, and un-noted once its row is stored


def keep(camera_id: str, track_id: str, at: datetime, jpeg: bytes) -> bool:
    """Keep an object's first picture: the file, then the row that names it.

    The file is noted for removal *before* it is written, and the note is taken away in the change that stores its row. A worker killed
    between the two leaves a file the sweep deletes, never one nothing knows about (#322)."""
    try:
        _, relative = _relative(camera_id, track_id, at)
    except (OSError, ValueError) as error:
        logger.warning("The first picture of an object on camera %s was not kept: %s", camera_id, error)
        return False
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO track_picture_removals(relative_path) VALUES(?)", (relative,))
    if write_file(camera_id, track_id, at, jpeg) is None:
        return False
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            try:
                stored = db.execute("INSERT OR IGNORE INTO track_keyframes(track_id,captured_at,jpeg,relative_path,size_bytes,camera_id) VALUES(?,?,x'',?,?,?)",
                                    (track_id, at.astimezone(timezone.utc).isoformat(), relative, len(jpeg), camera_id)).rowcount == 1
            except sqlite3.IntegrityError:
                stored = False  # The track went (its footage was deleted) between the frame and this: the note stays and the file goes
            kept = db.execute("SELECT relative_path FROM track_keyframes WHERE track_id=?", (track_id,)).fetchone()
            if kept is not None and kept["relative_path"] == relative:
                db.execute("DELETE FROM track_picture_removals WHERE relative_path=?", (relative,))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return stored


def _file(relative_path: str) -> Path:
    """The picture's file, checked to be a picture of ours inside a configured, mounted volume."""
    from .storage import _hot_file_path

    path = _hot_file_path(relative_path)
    if path.suffix != ".jpg" or path.parent.parent.name != FOLDER or not DAY_NAME.match(path.parent.name):
        raise ValueError("Not an object's picture")
    return path


def read(row: sqlite3.Row | None) -> bytes | None:
    """A ``track_keyframes`` row's picture, **unmasked**: from its file, or from the row itself for one stored before pictures were files.
    None when there is none to be had (no row, the file gone, its volume away). Callers draw the privacy masks before it goes anywhere."""
    if row is None:
        return None
    if not row["relative_path"]:
        return bytes(row["jpeg"]) or None
    try:
        path = _file(row["relative_path"])
        if path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except (OSError, ValueError):
        return None
    return data if data.startswith(b"\xff\xd8") else None


def totals(db: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Each camera's pictures kept as files: how many and how many bytes."""
    return {row["camera_id"]: {"files": int(row["files"]), "bytes": int(row["bytes"])} for row in db.execute("SELECT camera_id,files,bytes FROM track_picture_totals")}


def sweep() -> dict[str, int]:
    """Delete the files of pictures whose tracks have gone, a bounded number. Reads first: with nothing noted, nothing is written (#280)."""
    from .storage import StorageUnavailable

    done = {"removed": 0, "waiting": 0}
    with connect() as db:
        if not has_row(db, "SELECT 1 FROM track_picture_removals LIMIT 1"):
            return done
        settled = (datetime.now(timezone.utc) - timedelta(seconds=SETTLE_SECONDS)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        noted = [row[0] for row in db.execute("SELECT relative_path FROM track_picture_removals WHERE removed_at<? ORDER BY removed_at LIMIT ?", (settled, SWEEP_BATCH))]
    gone: list[str] = []
    folders: set[Path] = set()
    for relative in noted:
        try:
            path = _file(relative)
        except StorageUnavailable:
            done["waiting"] += 1  # Its disk is not mounted: kept on the list until it is
            continue
        except (OSError, ValueError):
            gone.append(relative)  # Not a path of ours, or its volume is no longer configured: nothing to delete
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            done["waiting"] += 1
            continue
        gone.append(relative)
        folders.add(path.parent)
    for folder in folders:  # A day's folder with nothing left in it
        try:
            folder.rmdir()
        except OSError:
            pass
    if gone:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.executemany("DELETE FROM track_picture_removals WHERE relative_path=? AND removed_at<?", [(relative, settled) for relative in gone])
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        done["removed"] = len(gone)
    return done
