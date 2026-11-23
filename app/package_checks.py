"""Bounds on checking an uploaded evidence or case package, so a small crafted upload cannot exhaust memory or disk (#503).

A zip can declare far more than it holds: repetitive JSON compresses about 1,000 to 1, so a 2 MB upload could declare a 2 GiB
``integrity.json``, which was inflated into memory and parsed (measured: 200 MB of JSON from a 204 KB upload took the web worker from
457 MB to 1,077 MB). Deeply nested JSON raised an uncaught ``RecursionError``. The archive's directory was read with no limit on its
members, and a case package's clips, each allowed 2 GiB of declared content, were copied to the system's temporary folder.

On disk, an upload was written beside the exports with no limit on how many at once and no regard for the recordings volume's headroom, and
one left by a web worker killed part-way was never removed and counted against the exports' budget for good.

Here:

- the archive's directory is sized from its end record before it is read: at most ``MAX_MEMBERS`` members;
- a JSON member is refused from its declared size, before it is inflated, past ``MAX_JSON_BYTES``; it is read with that bound whatever it
  declares, and nesting too deep for the parser is "invalid", not a server error;
- a case package and its clips together inflate at most the verification size limit (``Budget``);
- ``MAX_UPLOADS`` checks run at once in the web process; another is refused with 429;
- uploads and the copies of a case package's clips are written to ``VERIFY_DIRECTORY`` on the recordings volume, which the exports'
  budget does not count, never below the volume's headroom (refused part-way, the file removed), and what is left there is removed at start.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_MEMBERS = 1000  # A case keeps at most 500 items; an export has a file a camera and a few more
MAX_JSON_BYTES = 32 * 1024**2  # integrity.json, metadata.json, chain.json, case.json: a day of 16 cameras' chain is about 7 MB
MAX_UPLOADS = 2
DIRECTORY_ENTRY_BYTES = 46 + 1024  # A directory entry's fixed part, and room for its name and extra fields (ours are a few dozen bytes)
VERIFY_DIRECTORY = ".verify"  # On the recordings volume, beside the exports, not in their folder
ROOM_CHECK_EVERY = 16 * 1024**2  # How often an upload looks at the free space as it is written
LEGACY_DIRECTORY, LEGACY_PATTERN = "exports", ".verify-*.upload"  # Where uploads were written before: the exports folder (exports.EXPORT_DIRECTORY)


class PackageTooLarge(ValueError):
    """A package or member past one of the limits; ValueError, so the verifiers say "invalid package" with the reason."""


class Budget:
    """What one check may still inflate: a case package's own members and every clip inside it draw on the same budget, the verification
    size limit (``exports.MAX_VERIFY_PACKAGE_BYTES``, 2 GiB), not one each."""

    def __init__(self, limit: int) -> None:
        self.left = limit

    def take(self, declared: int) -> None:
        if declared > self.left:
            raise PackageTooLarge("uncompressed package exceeds the verification size limit")
        self.left -= declared


def check_directory(package: Any) -> None:
    """Refuse an archive whose directory lists more than ``MAX_MEMBERS`` members, or is larger than they could need, before ``ZipFile`` reads
    it into memory. ``package`` is a path or a seekable binary file; a file is left where it was."""
    handle = open(package, "rb") if not hasattr(package, "read") else package  # noqa: SIM115 - closed below when opened here
    try:
        start = handle.tell()
        try:
            end = zipfile._EndRecData(handle)  # The end record, with the zip64 one where there is one: what ZipFile itself reads first
        finally:
            handle.seek(start)
        if not end:
            raise zipfile.BadZipFile("File is not a zip file")
        members, size = end[zipfile._ECD_ENTRIES_TOTAL], end[zipfile._ECD_SIZE]
        if members > MAX_MEMBERS or size > MAX_MEMBERS * DIRECTORY_ENTRY_BYTES:
            raise PackageTooLarge(f"the package has more than {MAX_MEMBERS} files")
    finally:
        if handle is not package:
            handle.close()


def read_json(archive: zipfile.ZipFile, name: str, limit: int = MAX_JSON_BYTES) -> Any:
    """A JSON member, refused from its declared size before it is inflated, read with that bound whatever it declares, and parsed with
    nesting too deep for the parser counted as an invalid package."""
    info = archive.getinfo(name)
    if info.file_size > limit:
        raise PackageTooLarge(f"{name} is larger than a genuine package's ({info.file_size} bytes)")
    with archive.open(info) as member:
        data = member.read(limit + 1)
    if len(data) > limit:
        raise PackageTooLarge(f"{name} is larger than a genuine package's")
    try:
        return json.loads(data)
    except RecursionError:
        raise ValueError(f"{name} is nested too deeply") from None


# --- uploads -------------------------------------------------------------------------------------------------------------------------------

_uploads = 0
_uploads_lock = threading.Lock()


class Busy(RuntimeError):
    pass


class Slot:
    """One of the ``MAX_UPLOADS`` places for a package check in this process; Busy when they are all taken."""

    def __enter__(self) -> "Slot":
        global _uploads
        with _uploads_lock:
            if _uploads >= MAX_UPLOADS:
                raise Busy(f"{MAX_UPLOADS} package checks are running: try again when one has finished")
            _uploads += 1
        return self

    def __exit__(self, *exc: object) -> None:
        global _uploads
        with _uploads_lock:
            _uploads -= 1


def _recordings_root() -> Path:
    """``recording.recordings_path()``, without importing the recorder into the supervisor, which removes the leftovers at start."""
    return Path(os.environ.get("NVR_RECORDINGS_PATH", "/recordings")).resolve()


def directory() -> Path:
    return _recordings_root() / VERIFY_DIRECTORY


class NoRoom(OSError):
    pass


def ensure_room(folder: Path, more: int) -> None:
    """Refuse to write ``more`` bytes to the recordings volume when that would take it below its headroom, below which the recorder deletes
    the oldest footage (the exports' own floor, #362)."""
    from .exports import free_floor

    if shutil.disk_usage(folder).free - more < free_floor(folder):
        raise NoRoom("There is not enough free space on the recordings volume to check this package without the recorder deleting footage")


def remove_leftovers() -> int:
    """Remove what checks interrupted by a stopped or killed web worker left, at start, when nothing can be using it. Returns the bytes freed."""
    root = _recordings_root()
    found = [*(root / VERIFY_DIRECTORY).glob("*"), *(root / LEGACY_DIRECTORY).glob(LEGACY_PATTERN)]
    freed = 0
    for path in found:
        try:
            freed += path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("A leftover package upload could not be removed: %s", path.name, exc_info=True)
    if found:
        logger.warning("Removed %d package upload(s) left by an interrupted check, freeing %d MB", len(found), freed // (1024 * 1024))
    return freed
