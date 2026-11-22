"""Footage files with no database row, left when the recorder was killed (#484).

Retention, headroom, tiering and the forecast all work from ``recording_segments`` rows. When the recorder is killed, FFmpeg dies with it:
the segment it was writing is an unfinished file that never reaches FFmpeg's list, and anything it finished in the last two seconds has
not been indexed. The next start gives every camera a new run folder and never reads the old one's list again, so about half a minute of
video per camera per kill stayed on the disk for ever, counted by nothing.

Every few minutes the recorder goes over the run folders that no recording is using:

* the segments its list says were finished are indexed (they are footage, and now play, expire and count like any other);
* a segment file that is in no list and has no row is the one FFmpeg was writing when it died. It was never finished and cannot be played,
  and it is removed once it is old enough that nothing can still be writing it;
* what is still on disk without a row afterwards is counted, and the count is in ``/metrics``.

Only a camera's own run folders are ever looked at (a folder named as the recorder names them, under the camera's folder on a volume that
is present), and nothing is deleted but those unfinished segment files and a run folder left empty.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .store import connect

if TYPE_CHECKING:
    from .recording import RecordingManager

logger = logging.getLogger("nvr.recording")

RUN_NAME = re.compile(r"^[0-9a-f]{32}$")  # uuid4().hex: how the recorder names a run's folder
SEGMENT_NAME = re.compile(r"^segment-\d{8}T\d{6}Z\.mp4$")
SAFE_AGE_SECONDS = 300.0  # Nothing in a run folder is touched until everything in it has been quiet this long
FOLDERS_PER_PASS = 200  # A pass reads at most this many run folders; the next pass carries on


@dataclass
class Result:
    folders: int = 0
    indexed: int = 0
    removed_files: int = 0
    removed_bytes: int = 0
    unindexed_files: int = 0
    unindexed_bytes: int = 0


def _state_file() -> Path:
    from .workers import control_directory

    return control_directory() / "unindexed-footage.json"


def last_result() -> dict[str, int] | None:
    """What the last pass found, for the metrics page; None before the first pass."""
    try:
        found = json.loads(_state_file().read_text(encoding="utf-8"))
        return {key: int(found[key]) for key in ("unindexed_files", "unindexed_bytes", "checked_at")}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _camera_folders(camera_ids: list[str]) -> list[tuple[str, Path]]:
    """Each camera's folder on every volume that is present (a camera's group may have moved between volumes)."""
    from . import volume_marker
    from .storage import configured_volumes, volume_path

    found = []
    for volume_id in configured_volumes():
        try:
            base = volume_path(volume_id)
            if not base.is_dir() or not volume_marker.verified(volume_id, base):
                continue
        except (OSError, ValueError):
            continue
        found += [(camera_id, base / camera_id) for camera_id in camera_ids]
    return found


def _old_runs(camera_ids: list[str], active: set[Path], now: float) -> list[tuple[str, Path]]:
    runs = []
    for camera_id, folder in _camera_folders(camera_ids):
        try:
            with os.scandir(folder) as entries:
                candidates = [Path(entry.path) for entry in entries if RUN_NAME.match(entry.name) and entry.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for run in candidates:
            if run in active:
                continue  # A recording is using it
            try:
                with os.scandir(run) as files:
                    newest = max((entry.stat(follow_symlinks=False).st_mtime for entry in files), default=run.stat().st_mtime)
            except OSError:
                continue
            if now - newest >= SAFE_AGE_SECONDS:
                runs.append((camera_id, run))
    return sorted(runs, key=lambda item: str(item[1]))


def _listed(run: Path) -> set[str]:
    """The segments FFmpeg's list says were finished: the names on its complete lines."""
    try:
        lines = (run / "segments.csv").read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError):
        return set()
    return {line.split(",", 1)[0] for line in lines if line.endswith(("\n", "\r"))}


def _has_row(relative: str) -> bool:
    with connect() as db:
        return db.execute("SELECT 1 FROM recording_segments WHERE relative_path=?", (relative,)).fetchone() is not None


def reconcile(manager: "RecordingManager", camera_ids: list[str], active: set[Path], *, after: str = "", now: float | None = None) -> tuple[Result, str]:
    """One pass over up to ``FOLDERS_PER_PASS`` old run folders, starting after the folder ``after``. Returns what was done and where the
    next pass carries on ("" when every folder has been seen). Blocking: the recorder runs it in a thread. The disk is read with no
    database lock held; each run's new rows are one short transaction."""
    from .recording import recordings_path, remove_run_folder_if_empty

    now = time.time() if now is None else now
    root = recordings_path()
    result = Result()
    runs = [item for item in _old_runs(camera_ids, active, now) if str(item[1]) > after]
    for camera_id, run in runs[:FOLDERS_PER_PASS]:
        result.folders += 1
        try:
            result.indexed += manager._index_csv(camera_id, run / "segments.csv", run, must_store=True)
        except sqlite3.Error:
            continue  # The database is busy: this folder is looked at again next round
        finally:
            manager._indexed_lines.pop(run / "segments.csv", None)
        listed = _listed(run)
        try:
            with os.scandir(run) as files:
                segments = [(entry.name, entry.stat(follow_symlinks=False)) for entry in files if SEGMENT_NAME.match(entry.name) and entry.is_file(follow_symlinks=False)]
        except OSError:
            continue
        for name, status in segments:
            path = run / name
            try:
                relative = path.resolve().relative_to(root).as_posix()
            except ValueError:
                continue  # Not under the recordings folder: never touched
            if _has_row(relative):
                continue
            if name not in listed and now - status.st_mtime >= SAFE_AGE_SECONDS:
                # FFmpeg never finished it: no list line, no row, and nothing has written to it for minutes.
                try:
                    path.unlink()
                    result.removed_files += 1
                    result.removed_bytes += status.st_size
                except OSError:
                    pass
                continue
            result.unindexed_files += 1
            result.unindexed_bytes += status.st_size
        remove_run_folder_if_empty(run)
    following = str(runs[FOLDERS_PER_PASS - 1][1]) if len(runs) > FOLDERS_PER_PASS else ""
    return result, following


def record(result: Result) -> None:
    """Keep what a whole round found where the web process can read it, and say so in the log when there was anything."""
    if result.indexed or result.removed_files or result.unindexed_files:
        logger.info("Run folders left by an earlier recorder: %d segments indexed, %d unfinished files removed (%d bytes), %d files (%d bytes) still on disk without a row",
                    result.indexed, result.removed_files, result.removed_bytes, result.unindexed_files, result.unindexed_bytes)
    try:
        target = _state_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.{os.getpid()}")
        partial.write_text(json.dumps({"unindexed_files": result.unindexed_files, "unindexed_bytes": result.unindexed_bytes, "checked_at": int(time.time())}), encoding="utf-8")
        os.replace(partial, target)
    except OSError:
        pass
