"""Small, dependency-free password and session-token primitives."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from contextlib import contextmanager

_ITERATIONS = 600_000
_SALT_BYTES = 16


def password_problem(password: str) -> str | None:
    """What is wrong with a new local password, or None. One rule for first-run setup, a change and a host reset."""
    if len(password) < 14 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "Password must be at least 14 characters and include a letter and a number"
    if len(password) > 1024:
        return "Password must be at most 1024 characters"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# How many password checks may run at once for callers who are not signed in (#462). One check is about a tenth of a second of one
# processor core, on purpose, and runs in the pool of threads every ordinary request shares: unlimited, some 45 sign-in attempts a second
# kept every core hashing and every thread taken. Half the cores, between 2 and 8.
SIGN_IN_CHECKS_AT_ONCE = max(2, min(8, (os.cpu_count() or 2) // 2))
SIGN_IN_CHECK_WAIT_SECONDS = 2.0
_sign_in_checks = threading.BoundedSemaphore(SIGN_IN_CHECKS_AT_ONCE)


class SignInBusy(Exception):
    """Too many sign-in attempts are being checked at this moment; this one was not checked at all."""


@contextmanager
def sign_in_check():
    """Hold one of the few places in which a password from someone not signed in may be checked. Waits briefly for a place, then raises
    ``SignInBusy``: the caller answers 429 and does not count the attempt as a wrong password."""
    if not _sign_in_checks.acquire(timeout=SIGN_IN_CHECK_WAIT_SECONDS):
        raise SignInBusy()
    try:
        yield
    finally:
        _sign_in_checks.release()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """The stored form of a session, wall or challenge token. Every token the NVR issues is ASCII, for which this is the digest it always was;
    anything else a caller sends is digested too and simply matches nothing, where it used to raise and answer 500 (#371)."""
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


_decoy: str | None = None


def spend_password_time(password: str) -> None:
    """Do the work of checking a password against nothing, so refusing an unknown name takes as long as a wrong password."""
    global _decoy
    if _decoy is None:
        _decoy = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _decoy)
