"""Sign-in rate limiting, per account, per account-and-address pair, and per address.

Every attempt is counted before it is judged, in the same transaction that checks whether it
may go ahead, so parallel guesses cannot all slip past a check made before any was counted.
A success undoes its own count. After a few failures every further attempt waits, twice as
long each time, up to a cap: a guessing attacker is slowed to a handful of tries an hour, and
no one is ever locked out for longer than the cap.

Three buckets are kept. The pair (this account from this address) has a low threshold, so an
attacker is stopped quickly yet cannot keep the real person, signing in from elsewhere, waiting.
The account on its own has a high threshold, which spreading guesses over many addresses
reaches. The address has a high threshold too, for one machine trying many accounts. Failures
are forgotten after a quiet spell, and a completed sign-in clears the account's buckets. A
throttled attempt is refused even with the right credentials, or the throttle would not slow
guessing.

The account-wide wait is for callers from outside. With a handful of addresses an outsider
could otherwise keep an account's owner from signing in at all (#457). It never applies to a
caller on the local network (``network.request_is_remote``), nor to a browser in which a
sign-in to that account was completed before (``app.known_browser``): those are limited by
their own pair and address counts only, and their failures are not added to the account's.
From any other address a right password still waits, as before. When the account-wide wait
starts for an account that exists, an incident says someone is guessing.

IPv6 callers are counted by their /64: one host has a whole /64 of addresses to use.
"""
from __future__ import annotations

import ipaddress
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import HTTPException, Request

from . import known_browser, network
from .audit import record
from .incidents import sync_incident
from .store import connect
from app.clock import now as _now

# (failures allowed before waiting starts, first wait in seconds)
LIMITS = {"pair": (4, 2), "account": (19, 2), "address": (19, 2)}
MAX_WAIT_SECONDS = 300
FORGET_AFTER = timedelta(minutes=15)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Caller(str):
    """The caller's address (it is a string, and is logged as one), with the two things the limits also need to know about the request:
    whether it came from the local network, and which remembered-browser tokens it sent."""

    local: bool = False
    tokens: tuple[str, ...] = ()


def client_address(request: Request) -> Caller:
    """The caller's address: the peer's, or behind a trusted proxy (``NVR_TRUSTED_PROXIES``) the address it reports (see ``network``)."""
    caller = Caller(network.client_address(request))
    caller.local = not network.request_is_remote(request)
    caller.tokens = known_browser.tokens(request)
    return caller


def counted_as(address: str) -> str:
    """The address a caller is counted under: its own, or for IPv6 its /64."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    if parsed.version == 4:
        return str(parsed)
    if parsed.ipv4_mapped:  # ::ffff:198.51.100.7 is 198.51.100.7
        return str(parsed.ipv4_mapped)
    return str(ipaddress.ip_network(f"{parsed}/64", strict=False))


def _spared(db: sqlite3.Connection, account: str, address: str) -> bool:
    """Whether the account-wide wait leaves this caller alone: on the local network, or a browser that has signed in to this account."""
    if not account or not isinstance(address, Caller):
        return False
    return address.local or known_browser.knows(db, address.tokens, account)


def account_key(username: str) -> str:
    return username.strip().casefold()[:128]


def _buckets(account: str, address: str, include_address: bool = True, spared: bool = False) -> list[tuple[str, str]]:
    buckets = []
    address = counted_as(address) if address else address
    if account:
        buckets.append(("pair", f"{account}|{address}"))
        if not spared:
            buckets.append(("account", account))
    if include_address and address:
        buckets.append(("address", address))
    return buckets


def _wait(scope: str, failures: int) -> int:
    allowed, first = LIMITS[scope]
    if failures <= allowed:
        return 0
    return min(MAX_WAIT_SECONDS, first * 2 ** (failures - allowed - 1))


@dataclass(frozen=True)
class Throttle:
    scope: str
    retry_after: int


def _blocked(db: sqlite3.Connection, buckets: list[tuple[str, str]], now: datetime) -> Throttle | None:
    longest: Throttle | None = None
    for scope, key in buckets:
        row = db.execute("SELECT blocked_until FROM auth_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row and row["blocked_until"] and _parse(row["blocked_until"]) > now:
            wait = int((_parse(row["blocked_until"]) - now).total_seconds()) + 1
            if longest is None or wait > longest.retry_after:
                longest = Throttle(scope, wait)
    return longest


def _count(db: sqlite3.Connection, buckets: list[tuple[str, str]], address: str, now: datetime) -> None:
    forgotten = ((now - FORGET_AFTER).isoformat(), now.isoformat())
    for row in db.execute("SELECT key FROM auth_failures WHERE scope='account' AND last_failure_at<? AND (blocked_until IS NULL OR blocked_until<?)", forgotten).fetchall():
        _guessing(db, row["key"], active=False)  # Quiet for long enough: the incident that said someone was guessing recovers
    db.execute("DELETE FROM auth_failures WHERE last_failure_at<? AND (blocked_until IS NULL OR blocked_until<?)", forgotten)
    address_is_past_its_own_allowance = False
    for scope, key in buckets:
        row = db.execute("SELECT failures,last_failure_at FROM auth_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
        failures = 1 if row is None or now - _parse(row["last_failure_at"]) > FORGET_AFTER else int(row["failures"]) + 1
        if scope == "pair":
            address_is_past_its_own_allowance = failures > LIMITS["pair"][0]
        elif scope == "account" and address_is_past_its_own_allowance:
            # This address is already being slowed for this account. Its further tries are not added to the count that slows everyone: one
            # address sending a wrong password every few minutes used to fill it, and then the real person waited too (#365). Reaching the
            # account-wide limit takes guesses from several addresses, which is what that limit is for.
            continue
        wait = _wait(scope, failures)
        db.execute("INSERT INTO auth_failures(scope,key,failures,last_failure_at,blocked_until) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(scope,key) DO UPDATE SET failures=excluded.failures,last_failure_at=excluded.last_failure_at,blocked_until=excluded.blocked_until",
                   (scope, key, failures, now.isoformat(), (now + timedelta(seconds=wait)).isoformat() if wait else None))
        if wait and failures - 1 == LIMITS[scope][0] and _may_log(db, scope, key):
            record(db, None, "auth_throttled", scope, _logged_key(db, scope, key), failures=failures, wait_seconds=wait, address=address)
            if scope == "account":
                _guessing(db, key, active=True)


def _guessing(db: sqlite3.Connection, account: str, *, active: bool) -> None:
    """The incident that tells the owner someone is guessing an account's password from several addresses: raised when the account-wide
    wait starts, recovered when the account is signed in to or its failures are forgotten. Only for an account that exists: anything
    else typed in the username box is not an account under attack, and may be a password typed in the wrong box."""
    user = db.execute("SELECT username FROM users WHERE username=? COLLATE NOCASE", (account,)).fetchone()
    if user is None:
        return
    sync_incident(db, incident_key=f"signin_guessing:{account}", camera_id=None, fault_type="signin_guessing", active=active,
                  summary=f"Wrong passwords for the account {user['username']} are arriving from several addresses. Sign-in to it from outside the local network "
                          "is being slowed. People on the local network, and browsers that have signed in to it before, are not affected.",
                  details={"account": user["username"]})


def _may_log(db: sqlite3.Connection, scope: str, key: str) -> bool:
    """Whether a threshold row may be written: for an account that exists, at most ``security_log.FAILED_SIGN_IN_ROWS_PER_HOUR`` failure
    rows an hour (#459), so guesses from many addresses cannot fill the log. Addresses and unknown names are not bounded here."""
    if scope == "address":
        return True
    account = key.rpartition("|")[0] if scope == "pair" else key
    user = db.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE", (account,)).fetchone()
    if user is None:
        return True
    from .security_log import failure_rows_left
    return failure_rows_left(db, account, int(user["id"]))


def _logged_key(db: sqlite3.Connection, scope: str, key: str) -> str:
    """A throttle bucket's key as the security log may hold it: an account that exists by name, anything else typed in the username box
    as a digest (#367). People type their password there by mistake."""
    if scope == "address":
        return key
    account, bar, address = key.rpartition("|") if scope == "pair" else (key, "", "")
    if db.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (account,)).fetchone():
        return key
    from .security_log import unknown_name
    return unknown_name(account) + bar + address


def _attempt(account: str, address: str, include_address: bool) -> None:
    now = _now()
    with connect() as db:
        buckets = _buckets(account, address, include_address, _spared(db, account, address))
        waiting = _blocked(db, buckets, now)  # Read first: an attempt that is already being slowed is refused without taking the write lock (#353)
        if waiting is not None:
            raise HTTPException(status_code=429, detail=f"Too many failed attempts. Try again in {waiting.retry_after} seconds.",
                                headers={"Retry-After": str(waiting.retry_after)})
        db.execute("BEGIN IMMEDIATE")
        try:
            throttle = _blocked(db, buckets, now)
            if throttle is None:
                _count(db, buckets, address, now)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    if throttle:
        raise HTTPException(status_code=429, detail=f"Too many failed attempts. Try again in {throttle.retry_after} seconds.",
                            headers={"Retry-After": str(throttle.retry_after)})


def begin(account: str, address: str) -> None:
    """Count an attempt, or refuse it (429, with Retry-After) while the account, the pair or the address is waiting.

    The attempt counts as a failure until ``release`` or ``succeeded`` says otherwise. ``account`` may be empty
    when it is not yet known (a passkey on its own): only the address is counted."""
    _attempt(account, address, include_address=True)


def begin_account(account: str, address: str) -> None:
    """Count an attempt against an account that has just been identified, not the address (already counted by ``begin``)."""
    _attempt(account, address, include_address=False)


def record_failure(account: str, address: str) -> None:
    """Count a failure without judging it (for callers that already know it failed)."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _count(db, _buckets(account, address, spared=_spared(db, account, address)), address, _now())
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def release(account: str, address: str, include_address: bool = True) -> None:
    """Take back an attempt's count because it was right, leaving earlier failures in place."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for scope, key in _buckets(account, address, include_address, _spared(db, account, address)):
                row = db.execute("SELECT failures,last_failure_at FROM auth_failures WHERE scope=? AND key=?", (scope, key)).fetchone()
                if row is None:
                    continue
                failures = max(0, int(row["failures"]) - 1)
                if failures == 0:
                    db.execute("DELETE FROM auth_failures WHERE scope=? AND key=?", (scope, key))
                    continue
                # The attempt was let in, so nothing was waiting when it was made: taking it back leaves nothing waiting either. It used to start
                # a new wait from this moment, which refused the second-factor step that follows a right password (#365).
                db.execute("UPDATE auth_failures SET failures=?,blocked_until=NULL WHERE scope=? AND key=?", (failures, scope, key))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def succeeded(account: str, address: str) -> None:
    """A completed sign-in: this attempt's count goes, and the account's failures with it."""
    release(account, address)
    with connect() as db:
        db.execute("DELETE FROM auth_failures WHERE (scope='account' AND key=?) OR (scope='pair' AND key=?)", (account, f"{account}|{counted_as(address)}"))
        _guessing(db, account, active=False)
