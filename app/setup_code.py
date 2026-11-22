"""The one-time setup code (#542).

Creating the first administrator needed only a username and a password, so whoever reached the port first while no account existed
became the administrator: anyone on any network the host is on, the camera network included, and again whenever the server started on
an empty database (a lost volume, a wrong path, a restore gone wrong).

While no account exists the NVR now holds a random code. It is made afresh at each start, printed once in the log, and written to a
file only the service user can read, beside the credential key (``/secrets/setup-code`` in the container). Creating the first
administrator needs it, from any address. It stops working the moment the administrator exists, and the file is removed.

Wrong codes are limited: ``WRONG_A_MINUTE`` from one address and the same in all, after which every attempt is refused until the minute
has passed, the right code included (or the limit would not slow guessing). The code is never put in an API answer, an error or a log
row other than the one line that prints it.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

from .store import connect, has_row

logger = logging.getLogger(__name__)
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # No I, L, O, 0 or 1: it is read off a screen and typed
GROUPS, GROUP_LENGTH = 3, 4
WRONG_A_MINUTE = 5
WINDOW_SECONDS = 60.0
_lock = threading.RLock()  # Re-entrant: checking a code can make one (a process the supervisor did not start), which takes it again
_wrong: dict[str, dict[str, deque[float]]] = {}  # Code file -> address (or "" for everyone together) -> when each wrong code came


def path() -> Path:
    from .credentials import key_path

    return key_path().parent / "setup-code"


def _make() -> str:
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(GROUP_LENGTH)) for _ in range(GROUPS))


def _write(code: str) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{os.getpid()}")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # Readable by the service user alone
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(code + "\n")
    os.replace(partial, target)


def _read() -> str | None:
    try:
        return path().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def normalise(text: str) -> str:
    """A code as typed: spaces and dashes anywhere, either case."""
    return "".join(character for character in text.upper() if character.isalnum())


def needed() -> bool:
    with connect() as db:
        return not has_row(db, "SELECT 1 FROM users LIMIT 1")


def _looks_lost() -> str | None:
    """Why an empty database looks like a lost or misplaced one, not a new installation: footage is already on the recordings volume."""
    from .recording import recordings_path

    try:
        root = recordings_path()
        for folder in root.iterdir():
            if folder.is_dir() and any(folder.iterdir()):
                return f"there is already footage under {root}"
    except OSError:
        return None
    return None


def announce() -> str | None:
    """At start: with no account, make a new code, write it and print it once; with one, make sure no code is left. Returns the code made."""
    try:
        if not needed():
            path().unlink(missing_ok=True)
            return None
    except sqlite3.Error:
        return None  # The database cannot be read: nothing to announce, and the setup route will refuse
    code = _make()
    try:
        _write(code)
    except OSError as error:
        logger.error("The setup code could not be written to %s (%s): first-time setup cannot be completed until it can", path(), error)
        return None
    with _lock:
        _wrong.pop(str(path()), None)
    lost = _looks_lost()
    if lost:
        logger.error("This NVR has started with an empty database, but %s. The database looks lost or misplaced, not new: see docs/database-recovery.md "
                     "before doing first-time setup, which would start again with no cameras, accounts or history.", lost)
    logger.warning("No administrator exists yet. To create one, open the NVR in a browser and enter this setup code: %s  (it is also in %s, "
                   "and stops working once the administrator exists)", code, path())
    return code


def expected() -> str | None:
    """The code that completes setup now, made if there is none yet (a process that was not started by the supervisor). None once an
    account exists."""
    if not needed():
        return None
    return _read() or announce()


class Refused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


WHERE = "It is printed in the NVR's log when it starts with no administrator: run 'docker compose logs nvr' on the server and look for \"setup code\"."


def check(code: str, address: str) -> None:
    """Raise ``Refused`` unless ``code`` is the setup code and this attempt is within the limits."""
    key, now = str(path()), time.monotonic()
    with _lock:
        counted = _wrong.setdefault(key, {})
        for bucket in (address, ""):
            recent = counted.setdefault(bucket, deque())
            while recent and now - recent[0] > WINDOW_SECONDS:
                recent.popleft()
            if len(recent) >= WRONG_A_MINUTE:
                raise Refused(429, "Too many wrong setup codes. Wait a minute and try again.")
        right = expected()
        if right is not None and code and hmac.compare_digest(normalise(code).encode(), normalise(right).encode()):
            return
        counted[address].append(now)
        counted[""].append(now)
    raise Refused(403, f"That is not the setup code. {WHERE}")


def used() -> None:
    """The administrator exists: the code is gone for good."""
    path().unlink(missing_ok=True)
    with _lock:
        _wrong.pop(str(path()), None)
