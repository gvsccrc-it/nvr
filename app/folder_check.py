"""Can the NVR write to the folders it was given? (#551)

The NVR runs as an unprivileged user (id 10001 in the image) with every Linux capability dropped. Docker gives a named volume the
ownership of the image's folder, but never changes a folder mounted in from the host: the recommended place for backups, on another
disk, then fails with "permission denied" at the first backup, days later and with nothing saying why.

``check`` tries each configured folder at start-up. For each one it cannot write to, one plain line in the log names the folder, the
user the NVR runs as and the command that fixes it, and an incident is raised; the incident recovers at a start that can write there.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)
FAULT_TYPE = "folder_not_writable"


def folders() -> dict[str, Path]:
    """What each folder is for and where it is: the database's, the secrets', each recordings volume and the backups'."""
    from . import backup
    from .credentials import key_path
    from .recording import recordings_path
    from .storage import configured_volumes
    from .store import database_path

    found = {"the database": database_path().parent, "secrets": key_path().parent, "backups": backup.destination()}
    try:
        for name, relative in configured_volumes().items():
            found[f"recordings ({name})"] = recordings_path() / relative
    except ValueError:
        found["recordings (default)"] = recordings_path()
    return found


def writable(folder: Path) -> str | None:
    """None when a file can be made and removed there, else why not. A folder that does not exist is not this check's business: a
    recordings volume that is not mounted has its own state and incident."""
    if not folder.is_dir():
        return None
    probe = folder / f".nvr-write-check-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return None
    except OSError as error:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return error.strerror or str(error)


def check() -> dict[str, str]:
    """Try every folder, log a line for each that cannot be written, and raise or recover its incident. Returns the ones that failed and
    why. Never raises: a start is not stopped by this, the work that needs the folder says so when it comes to it."""
    from .incidents import sync_incident
    from .store import connect

    user = f"{os.getuid()}:{os.getgid()}"
    failed: dict[str, str] = {}
    results: list[tuple[str, Path, str | None]] = []
    for purpose, folder in folders().items():
        reason = writable(folder)
        results.append((purpose, folder, reason))
        if reason is not None:
            failed[purpose] = reason
            logger.error("The NVR cannot write to its %s folder %s (%s). It runs as user %s and a folder mounted from the host keeps the host's owner: "
                         "on the host run 'sudo chown -R %s <that folder>', then restart the NVR.", purpose.split(" (")[0], folder, reason, user, user)
    try:
        with connect() as db:
            if not failed and not db.execute("SELECT 1 FROM incidents WHERE fault_type=? AND fault_active=1 LIMIT 1", (FAULT_TYPE,)).fetchone():
                return failed  # Nothing is wrong and nothing was raised: nothing to write
            db.execute("BEGIN IMMEDIATE")
            try:
                for purpose, folder, reason in results:
                    sync_incident(db, incident_key=f"folder:{purpose}", camera_id=None, fault_type=FAULT_TYPE, active=reason is not None,
                                  summary=(f"The NVR cannot write to its {purpose} folder ({reason}). It runs as user {user}; give that user the folder on the host "
                                           f"(sudo chown -R {user} <the folder>) and restart the NVR." if reason is not None else f"The NVR can write to its {purpose} folder again"),
                                  details={"purpose": purpose, "folder": str(folder), "reason": reason, "runs_as": user})
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
    except sqlite3.Error:
        logger.exception("The folder check could not record what it found")  # (The database's own folder may be the one that cannot be written)
    return failed
