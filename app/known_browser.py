"""A browser that has signed in to an account before is remembered, for the sign-in limits only (#457).

When guesses at one account's password arrive from many addresses, every caller from outside is made to wait, the account's owner
included: with a handful of addresses an outsider could keep an account from being signed in to at all. The owner's own browser is
told apart by a cookie it was given the last time a sign-in to that account was completed in it. A browser holding one is limited
only by its own counts, never by the account-wide wait.

The cookie proves nothing else. It is not a session and opens nothing: the password, and the second factor, are asked for as always.
It is HttpOnly and SameSite=Strict, names the account it is for, and stops counting when the password changes (it is made from the
password's stored hash) or the account is disabled. A browser can hold one for each of a few accounts.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time

from fastapi import Request, Response

from app import credentials

COOKIE = "nvr_known_browser"
DAYS = 180
MOST_ACCOUNTS = 8


def _mac(user_id: int, issued: int, password_hash: str) -> str:
    made_from = f"{user_id}|{issued}|{hashlib.sha256(password_hash.encode('utf-8')).hexdigest()}".encode("ascii")
    return hmac.new(credentials.derived_key("known-browser"), made_from, hashlib.sha256).hexdigest()


def tokens(request: Request) -> tuple[str, ...]:
    """What the browser sent, unjudged: at most a few short tokens."""
    return tuple(item for item in request.cookies.get(COOKIE, "").split("~") if 0 < len(item) <= 128)[:MOST_ACCOUNTS]


def knows(db: sqlite3.Connection, sent: tuple[str, ...], account: str) -> bool:
    """Whether one of the tokens a browser sent was given to it for this account, and still stands."""
    if not sent or not account:
        return False
    user = db.execute("SELECT id,password_hash FROM users WHERE username=? COLLATE NOCASE AND disabled_at IS NULL AND provider_issuer IS NULL", (account,)).fetchone()
    if user is None:
        return False
    now = int(time.time())
    for token in sent:
        user_id, _, rest = token.partition(".")
        issued, _, mac = rest.partition(".")
        if user_id != str(user["id"]) or not issued.isdigit() or not 0 <= now - int(issued) <= DAYS * 86400:
            continue
        if hmac.compare_digest(mac, _mac(int(user["id"]), int(issued), user["password_hash"])):
            return True
    return False


def remember(db: sqlite3.Connection, sent: tuple[str, ...], response: Response, user_id: int, *, secure: bool) -> None:
    """A sign-in to this account was completed in this browser: give it the cookie that says so, beside any it holds for other accounts."""
    user = db.execute("SELECT password_hash FROM users WHERE id=? AND provider_issuer IS NULL", (user_id,)).fetchone()
    if user is None:
        return  # Not a local account: its sign-in is not ours to limit
    issued = int(time.time())
    kept = [token for token in sent if token.partition(".")[0] != str(user_id)]
    held = [*kept, f"{user_id}.{issued}.{_mac(user_id, issued, user['password_hash'])}"][-MOST_ACCOUNTS:]
    response.set_cookie(COOKIE, "~".join(held), httponly=True, secure=secure, samesite="strict", path="/", max_age=DAYS * 86400)
