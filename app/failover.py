"""Failover recorder (#143): a standby NVR that records in the primary's place while the primary is down, and hands the footage back.

**Roles.** An NVR is ``primary`` (the one that normally records), ``standby`` (waits and watches) or ``off``. They share a secret; every message between them is
signed with it (HMAC-SHA256 over the time, method, path and body digest, valid for a minute).

**Lease.** The primary renews a lease with the standby every few seconds. While the standby hears the renewals it stays quiet: its recording is *held* (a camera
whose recording is held behaves as though recording were disabled, reason ``failover_standby``). After ``silence_seconds`` without a renewal (always longer than the
lease) the standby *takes over*: it increments the epoch, stops holding its recording and records every camera of the configuration it copied from the primary.

**Configuration.** The standby fetches the primary's encrypted backup on a schedule and copies the configuration needed to record (cameras, groups, schedules,
retention, masks) from it. It never copies footage, the footage index or the signing key. It needs the primary's credential key, so a standby is first set up by restoring
a backup of the primary on it once (``python -m app.backup restore``); the passphrase for backups is the same on both.

**Handback.** When the primary comes back (or its renewals get through again) and the standby is recording, the standby stops at once and answers "not yet" until its
recorders have stopped, then grants the lease and offers its footage. A primary never records while it has not been granted the lease after starting (it waits a short
grace for an answer; a standby that cannot be reached at all is assumed down), so both never record at once after a restart. The primary imports the footage
(``source='failover'``), where the footage chain picks it up like any other, and tells the standby, which then recovers.

**Partitions.** A primary that is running but cannot reach the standby keeps recording (a standby that is merely down must not stop it), so during a network partition both
may record the same cameras: the standby takes over, the primary carries on, and the first renewal that gets through ends the standby's takeover and imports its footage.
That overlap is the price of never losing recording to a failure of the standby; it is documented in docs/failover-recorder.md.

**Incidents.** Takeover raises an incident on the standby when it happens and on the primary when it learns of it; both recover when the handback is complete.

**The link (#502, part 2).** The two talk over HTTPS by default: each NVR serves the failover endpoints on a port of their own with a
certificate it made for itself (``app.failover_link``, served by ``app.failover_listener``), and each pins the other's certificate by its
fingerprint when they are paired. Plain HTTP works only while "Allow an unencrypted link" is ticked on both: this NVR does not send to an
``http://`` address, nor answer a request that reached it unencrypted, unless its own box is ticked. A pair set up before that release
keeps working as it was (``legacy_link``) until its address is changed, and the hardening page lists it.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from . import backup
from .credentials import decrypt, key_path
from .failover_link import CertificateMismatch, pinned_transport
from .incidents import sync_incident
from .store import connect, database_path, has_row
from .webhooks import validate_target_url
from app.clock import now as _now

logger = logging.getLogger("nvr.failover")

ROLES = ("off", "primary", "standby")
SIGNATURE_WINDOW_SECONDS = 60
TIMEOUT = httpx.Timeout(20.0, connect=4.0)
STARTUP_GRACE_SECONDS = 20.0  # How long a starting primary waits for the standby's answer before it assumes the standby is down and records
STOP_WAIT_SECONDS = 30.0  # How long a standby waits for its recorders to stop before it grants the lease anyway
TICK_SECONDS = 2.0
POLL_WHILE_HELD_SECONDS = 3.0
MIN_SECRET_LENGTH = 16
MAX_SEGMENT_SECONDS = 4 * 3600
MAX_FOOTAGE_BYTES = 128 * 1024**3  # Larger than any segment: four hours at a high bitrate
RECORDING_STATES = ("recording", "buffering", "connecting", "reconnecting", "starting", "unavailable", "failed")
# What is copied from the primary's configuration: tables replaced whole, and the cameras' configuration columns (the rest of a camera row is this NVR's own state).
REPLACED_TABLES = ("camera_group_memberships", "recording_policy_overrides", "retention_policy_overrides", "privacy_masks")
CAMERA_COLUMNS = (
    "name", "onvif_uuid", "group_id", "camera_ip", "camera_brand", "device_service_url", "onvif_profile_token", "live_substream_profile_token", "archived_at",
    "stream_url_ciphertext", "live_substream_url_ciphertext", "audio_supported", "audio_enabled", "enabled", "recording_mode", "recording_enabled", "motion_service_url",
    "motion_credentials_ciphertext", "motion_pre_roll_seconds", "motion_post_roll_seconds", "motion_fallback_enabled", "motion_zones_json", "motion_sensitivity",
    "video_codec", "video_width", "video_height",
)


class PeerError(Exception):
    """A request from the peer that is refused; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- settings and state -------------------------------------------------------------------------------------------------------------------------

def read_settings(db: sqlite3.Connection) -> dict[str, Any]:
    row = db.execute("SELECT * FROM failover_settings WHERE singleton_id=1").fetchone()
    return {"role": row["role"], "peer_url": row["peer_url"], "has_secret": row["secret_ciphertext"] is not None, "allow_private": bool(row["allow_private"]),
            "silence_seconds": int(row["silence_seconds"]), "lease_seconds": int(row["lease_seconds"]), "sync_seconds": int(row["sync_seconds"]),
            "instance_id": row["instance_id"], "updated_at": row["updated_at"],
            # The link (#502, part 2): the other NVR's pinned certificate, "Allow an unencrypted link", and a pair kept as it was from before
            "peer_fingerprint": row["peer_fingerprint"], "allow_unencrypted": bool(row["allow_unencrypted"]), "legacy_link": bool(row["legacy_link"])}


def read_state(db: sqlite3.Connection) -> dict[str, Any]:
    return dict(db.execute("SELECT * FROM failover_state WHERE singleton_id=1").fetchone())


def shared_secret(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT secret_ciphertext FROM failover_settings WHERE singleton_id=1").fetchone()
    return decrypt(row["secret_ciphertext"]) if row and row["secret_ciphertext"] is not None else None


def _set_state(db: sqlite3.Connection, **fields: Any) -> None:
    columns = ",".join(f"{name}=?" for name in fields)
    db.execute(f"UPDATE failover_state SET {columns},updated_at=? WHERE singleton_id=1", (*fields.values(), _stamp()))


def held_reason(db: sqlite3.Connection, now: datetime | None = None) -> str | None:
    """Why this NVR must not record now (a coverage reason code), or None when it may.

    A standby records only while it has taken over; a primary does not record while it waits to hear from the standby after starting, or while the standby holds the
    takeover. Read by the recorder on every pass, from the database, so it holds across processes."""
    try:
        row = db.execute("SELECT s.role,t.takeover_active,t.yield_until FROM failover_settings s,failover_state t WHERE s.singleton_id=1 AND t.singleton_id=1").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None  # A database from before failover
        raise  # Any other failure is not a licence to record: the caller's pass is tried again
    if row is None or row["role"] == "off":
        return None
    if row["role"] == "standby":
        return None if row["takeover_active"] else "failover_standby"
    until = _parse(row["yield_until"])
    return "failover_yield" if until and until > (now or _now()) else None


def prepare_startup() -> None:
    """A primary that starts does not record until it has heard from the standby (or waited ``STARTUP_GRACE_SECONDS`` without hearing). Called before the recorder starts."""
    remove_leftovers()
    with connect() as db:
        if read_settings(db)["role"] == "primary":
            _set_state(db, yield_until=_stamp(_now() + timedelta(seconds=STARTUP_GRACE_SECONDS)), peer_state="starting")


# --- signing --------------------------------------------------------------------------------------------------------------------------------------

def _message(timestamp: str, method: str, path: str, body: bytes) -> bytes:
    return f"{timestamp}\n{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}".encode("utf-8")


NONCE_HEADER, SIGNATURE_2_HEADER = "X-Failover-Nonce", "X-Failover-Signature-2"


def _hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def sign(secret: str, method: str, path: str, body: bytes = b"", timestamp: int | None = None, nonce: str | None = None) -> dict[str, str]:
    """The headers of a signed request. ``X-Failover-Signature`` is the signature every version reads; ``X-Failover-Signature-2`` also covers a
    one-time value (``X-Failover-Nonce``), so that a captured request cannot be sent again (#502). Both are sent, so that an NVR that has not
    been upgraded yet still hears this one."""
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or secrets.token_hex(16)
    message = _message(stamp, method, path, body)
    with_nonce = _hmac(secret, _with_nonce(message, nonce))
    return {"X-Failover-Timestamp": stamp, "X-Failover-Signature": f"sha256={_hmac(secret, message)}", NONCE_HEADER: nonce, SIGNATURE_2_HEADER: f"sha256={with_nonce}"}


def _with_nonce(message: bytes, nonce: str) -> bytes:
    return message + b"\n" + nonce.encode("ascii")


def _problem(expected: str, signature: str | None, timestamp: str | None, now: float | None) -> str | None:
    """None when ``signature`` is ``expected`` and fresh; otherwise "signature" or, for a signature that is right but out of time, "clock:<seconds>"."""
    if not signature or not signature.startswith("sha256=") or not hmac.compare_digest(signature, expected):
        return "signature"
    difference = (time.time() if now is None else now) - int(timestamp or 0)
    return f"clock:{round(difference)}" if abs(difference) > SIGNATURE_WINDOW_SECONDS else None


def request_problem(secret: str | None, method: str, path: str, body: bytes, headers: Any, now: float | None = None) -> str | None:
    """Why a request is not to be obeyed, or None: "signature", "clock:<seconds>" (signed right, but the two clocks differ), "replay" (its
    one-time value was seen before) or "outdated" (signed right, by an NVR that does not send the one-time value yet)."""
    timestamp, nonce = headers.get("x-failover-timestamp"), headers.get(NONCE_HEADER.lower()) or ""
    if not secret or not timestamp:
        return "signature"
    try:
        int(timestamp)
    except ValueError:
        return "signature"
    message = _message(timestamp, method, path, body)
    if not nonce:
        problem = _problem(f"sha256={_hmac(secret, message)}", headers.get("x-failover-signature"), timestamp, now)
        return problem or "outdated"
    if not 16 <= len(nonce) <= 64 or not nonce.isalnum():
        return "signature"
    problem = _problem("sha256=" + _hmac(secret, _with_nonce(message, nonce)), headers.get(SIGNATURE_2_HEADER.lower()), timestamp, now)
    if problem:
        return problem
    return None if _first_use(nonce, now) else "replay"


_seen_nonces: dict[str, float] = {}
_nonce_lock = threading.Lock()


def _first_use(nonce: str, now: float | None = None) -> bool:
    """Whether this one-time value has not been seen within the signature window, and note it. A value older than the window is refused by
    its timestamp anyway, so nothing need be kept longer."""
    now = time.time() if now is None else now
    with _nonce_lock:
        if len(_seen_nonces) > 10_000:
            for key in [key for key, until in _seen_nonces.items() if until < now]:
                del _seen_nonces[key]
        if _seen_nonces.get(nonce, 0) >= now:
            return False
        _seen_nonces[nonce] = now + 2 * SIGNATURE_WINDOW_SECONDS
        return True


def verify(secret: str | None, method: str, path: str, body: bytes, timestamp: str | None, signature: str | None, now: float | None = None) -> bool:
    """The signature every version sends (without its one-time value)."""
    if not secret or not timestamp:
        return False
    try:
        int(timestamp)
    except ValueError:
        return False
    return _problem(f"sha256={_hmac(secret, _message(timestamp, method, path, body))}", signature, timestamp, now) is None


def sign_answer(secret: str, path: str, body: bytes, request_signature: str, timestamp: int | None = None) -> dict[str, str]:
    """Headers that prove an answer came from the holder of the secret, for this request and no other (#343). Requests were signed and
    answers were not, so anything able to alter the traffic between the two NVRs could answer a renewal "not granted" and stop the
    primary recording. The request's own signature is part of what is signed, so an old answer cannot be replayed to a new request."""
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(secret.encode("utf-8"), _message(stamp, "ANSWER", path, body) + b"\n" + request_signature.encode("utf-8", "replace"), hashlib.sha256).hexdigest()
    return {"X-Failover-Timestamp": stamp, "X-Failover-Signature": f"sha256={digest}"}


def answer_problem(secret: str | None, path: str, body: bytes, request_signature: str, timestamp: str | None, signature: str | None, now: float | None = None) -> str | None:
    """Why an answer is not to be believed, or None: "signature" or "clock:<seconds>"."""
    if not secret or not timestamp:
        return "signature"
    try:
        stamped = int(timestamp)
    except ValueError:
        return "signature"
    return _problem(sign_answer(secret, path, body, request_signature, stamped)["X-Failover-Signature"], signature, timestamp, now)


def verify_answer(secret: str | None, path: str, body: bytes, request_signature: str, timestamp: str | None, signature: str | None, now: float | None = None) -> bool:
    return answer_problem(secret, path, body, request_signature, timestamp, signature, now) is None


def footage_summary(digest: str, size: int) -> bytes:
    """What the standby signs for a footage file: its SHA-256 and size, the body itself being checked against them as it arrives."""
    return f"{digest}\n{int(size)}".encode("ascii")


def clock_message(problem: str, whose: str) -> str:
    """What to tell the administrator about a signature that is right but out of time: the clocks, not the secrets (#502)."""
    seconds = abs(int(problem.split(":", 1)[1]))
    return (f"{whose} is signed with the right secret, but the two NVRs' clocks differ by about {seconds} seconds (at most {SIGNATURE_WINDOW_SECONDS} "
            "are allowed): set both to the same time source (NTP)")


def authenticate(method: str, path: str, body: bytes, headers: Any) -> None:
    """Refuse a request that is not signed with this NVR's failover secret, or that was sent before (the same answer however it is wrong,
    except that a request signed right but out of time says the clocks differ, and one from an NVR not yet upgraded says so)."""
    with connect() as db:
        secret = shared_secret(db)
    problem = request_problem(secret, method, path, body, headers)
    if problem is None:
        return
    if problem.startswith("clock:"):
        message = clock_message(problem, "The other NVR's request")
        with connect() as db:
            _set_state(db, last_error=message)
        raise PeerError(401, message)
    if problem == "outdated":
        note_outdated_peer()
        raise PeerError(426, "The other NVR signs its requests the old way: upgrade both NVRs to the same version")
    if problem == "replay":
        raise PeerError(401, "The request was sent before")
    raise PeerError(401, "The request is not signed with this NVR's failover secret")


def note_outdated_peer() -> None:
    """A request signed right by a primary that has not been upgraded yet. It is refused, but it shows the primary is alive: a standby that
    has not taken over does not take over because of the upgrade, while the primary is recording."""
    with connect() as db:
        if read_settings(db)["role"] == "standby" and not read_state(db)["takeover_active"]:
            _set_state(db, last_renewed_at=_stamp(), last_contact_at=_stamp(),
                       last_error="The primary has not been upgraded: its requests are refused until both NVRs run the same version")


# --- the link: HTTPS with the other NVR's certificate pinned, or plain HTTP where both allow it (#502, part 2) -----------------------------------

CERTIFICATE_KEY = "failover:certificate"
UNENCRYPTED_OUT = ("The other NVR's address is plain http://, and this NVR does not allow an unencrypted failover link: enter the address of the "
                   "other NVR's HTTPS failover link (https://<address>:8443) and its certificate fingerprint, or tick \"Allow an unencrypted link\" on both NVRs")
UNENCRYPTED_IN = ("This NVR does not accept the failover link unencrypted: use the address of its HTTPS failover link and its certificate fingerprint, "
                  "or tick \"Allow an unencrypted link\" on both NVRs")


class LinkRefused(ValueError):
    """This NVR will not send to the other one over the link as it is set up (plain HTTP without "Allow an unencrypted link")."""


def unencrypted_allowed(settings: dict[str, Any]) -> bool:
    """Whether this NVR may use plain HTTP with the other: "Allow an unencrypted link" ticked, or a pair kept as it was from before #502."""
    return bool(settings.get("allow_unencrypted") or settings.get("legacy_link"))


def link_client(settings: dict[str, Any], timeout: httpx.Timeout) -> httpx.AsyncClient:
    """A client for the other NVR, as the link is set up: pinned to its certificate for an ``https://`` address with a fingerprint, plain
    only where this NVR allows an unencrypted link. An ``https://`` address without a fingerprint (one through a proxy with a public
    certificate, as could be set up before #502) is verified against the usual certificate authorities, as it always was."""
    scheme = str(settings.get("peer_url") or "").split(":", 1)[0].lower()
    if scheme == "http" and not unencrypted_allowed(settings):
        raise LinkRefused(UNENCRYPTED_OUT)
    if scheme == "https" and settings.get("peer_fingerprint"):
        return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False, transport=pinned_transport(str(settings["peer_fingerprint"])))
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)


def mismatch_message(exc: CertificateMismatch) -> str:
    return (f"{exc}. The connection was refused. If the other NVR's certificate was replaced (a new installation, its credential key restored "
            "from a backup, or a new certificate made on its failover page), pair the two again with the fingerprint shown on its failover page; "
            "otherwise something between the two NVRs may be intercepting the link")


def certificate_incident(db: sqlite3.Connection, active: bool, message: str | None = None) -> None:
    """Raised when the other NVR presents a certificate other than the pinned one, recovered at the next exchange it verifies."""
    if active or has_row(db, "SELECT 1 FROM incidents WHERE incident_key=? AND fault_active=1", (CERTIFICATE_KEY,)):
        summary = ("The other failover NVR presented a certificate that is not the one it was paired with: the failover link is refused" if active
                   else "The other failover NVR presents the certificate it was paired with again")
        sync_incident(db, incident_key=CERTIFICATE_KEY, camera_id=None, fault_type="failover", summary=summary, active=active, details={"error": message} if message else {})


def note_certificate(problem: CertificateMismatch | None) -> None:
    """A refused certificate (an incident, and the reason on the status), or a verified exchange (the incident recovers)."""
    with connect() as db:
        if problem is not None:
            message = mismatch_message(problem)
            logger.warning("The failover link was refused: %s", message)  # Fingerprints only: they are public
            _set_state(db, peer_state="wrong_certificate", last_error=message)
            certificate_incident(db, True, message)
        else:
            certificate_incident(db, False)


def refuse_unencrypted() -> None:
    """For a request signed right that reached this NVR unencrypted: allowed only while this NVR allows an unencrypted link. A refused
    one still shows the primary is alive, so a standby does not take over from a recording primary while the two are being moved to HTTPS."""
    with connect() as db:
        settings = read_settings(db)
        if unencrypted_allowed(settings):
            return
        if settings["role"] == "standby" and not read_state(db)["takeover_active"]:
            _set_state(db, last_renewed_at=_stamp(), last_contact_at=_stamp(), last_error="The primary sends its requests unencrypted, which this NVR does not allow: " + UNENCRYPTED_IN)
    raise PeerError(403, UNENCRYPTED_IN)


FINGERPRINT_REQUIRED = ("Enter the other NVR's certificate fingerprint: it is shown on the other NVR's failover page. Every https:// pair "
                        "pins the other NVR's certificate")


def link_warning(db: sqlite3.Connection) -> dict[str, Any] | None:
    """For the hardening page: this NVR's failover link where it is, or may be, unencrypted, or an https:// pair kept from before fingerprints
    were required that trusts public certificate authorities instead of pinning the other NVR (#502). None when neither."""
    settings = read_settings(db)
    if settings["role"] == "off" or not settings["peer_url"]:
        return None
    sends = str(settings["peer_url"]).lower().startswith("http://")
    accepts = unencrypted_allowed(settings)
    unpinned = str(settings["peer_url"]).lower().startswith("https://") and not settings["peer_fingerprint"]
    if not (sends or accepts or unpinned):
        return None
    return {"peer_url": settings["peer_url"], "sends_unencrypted": sends, "accepts_unencrypted": accepts, "unpinned": unpinned,
            "permission": "allowed" if settings["allow_unencrypted"] else "kept_from_upgrade"}


def check_peer_url(url: str, allow_private: bool) -> str:
    try:
        return validate_target_url(url, allow_private).rstrip("/")
    except ValueError as exc:
        raise ValueError(str(exc).replace("Webhook", "Peer").replace("webhook", "peer")) from None


# --- incidents and episodes -----------------------------------------------------------------------------------------------------------------------

TAKEOVER_KEY = "failover:takeover"
YIELD_KEY = "failover:yield"
# How long a primary goes on yielding to a standby that keeps saying "not yet". A standby stops its recorders and grants within
# STOP_WAIT_SECONDS; one still refusing after this is not handing back, and a primary that records nothing is the worse failure.
YIELD_LIMIT_SECONDS = 300.0


def _takeover_incident(db: sqlite3.Connection, role: str, active: bool, details: dict[str, Any], *, waiting: bool = False) -> None:
    """``waiting``: the standby has stopped recording for a primary that is back, and still holds footage the primary has not collected."""
    summary = ("The primary NVR is back: this standby has stopped recording and is keeping the footage it recorded until the primary has collected it" if waiting
               else "The primary NVR stopped answering: this standby is recording in its place" if role == "standby"
               else "The standby NVR recorded while this primary was away: its footage is being brought back") if active else \
              ("The primary NVR is back and recording again: the standby has handed back" if role == "standby" else "The standby's footage is back in this NVR and it is recording again")
    sync_incident(db, incident_key=TAKEOVER_KEY, camera_id=None, fault_type="failover", summary=summary, active=active, details={"role": role, **details})


def _yield_incident(db: sqlite3.Connection, active: bool, since: str | None = None) -> None:
    minutes = int(YIELD_LIMIT_SECONDS // 60)
    summary = (f"The standby NVR did not hand recording back within {minutes} minutes: this primary is recording again, and both may be recording" if active
               else "The standby NVR has handed recording back to this primary")
    if active or has_row(db, "SELECT 1 FROM incidents WHERE incident_key=? AND fault_active=1", (YIELD_KEY,)):
        sync_incident(db, incident_key=YIELD_KEY, camera_id=None, fault_type="failover", summary=summary, active=active, details={"role": "primary", "yielding_since": since})


def episodes(db: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute("SELECT * FROM failover_episodes ORDER BY started_at DESC,epoch DESC LIMIT ?", (limit,))]


def annotate_gaps(db: sqlite3.Connection, gaps: list[dict[str, Any]]) -> None:
    """Say, on each gap that overlaps the time before a standby took over, that the standby was waiting for the primary to be silent long enough."""
    found = [row for row in db.execute("SELECT silent_since,started_at,role FROM failover_episodes WHERE silent_since IS NOT NULL ORDER BY started_at DESC LIMIT 20")]
    if not found:
        return
    for gap in gaps:
        for row in found:
            if gap["starts_at"] < row["started_at"] and gap["ends_at"] > row["silent_since"]:
                gap["failover"] = {"message": "The standby NVR waits for the primary to be silent before it takes over recording; footage it recorded after that is in the timeline"}
                break


# --- the standby's side of the conversation -------------------------------------------------------------------------------------------------------

def recorders_idle(db: sqlite3.Connection) -> bool:
    marks = ",".join("?" * len(RECORDING_STATES))
    return db.execute(f"SELECT 1 FROM cameras WHERE enabled=1 AND recording_status IN ({marks}) LIMIT 1", RECORDING_STATES).fetchone() is None


_EPISODE_SEGMENTS = "source='live' AND unavailable_at IS NULL AND ends_at>? AND starts_at<?"


def _episode_range(db: sqlite3.Connection, episode: sqlite3.Row) -> tuple[str, str]:
    """From when to when the standby's footage belongs to this takeover: from its start up to the next takeover, if there was one before this
    episode's footage was collected (#498), since that footage is the next episode's."""
    following = db.execute("SELECT MIN(started_at) FROM failover_episodes WHERE role='standby' AND epoch>?", (episode["epoch"],)).fetchone()[0]
    return episode["started_at"], following or episode["ended_at"] or "9999"


def _segments_of(db: sqlite3.Connection, episode: sqlite3.Row) -> list[sqlite3.Row]:
    """Footage the standby recorded since it took over (a segment that began just before is its own: the recorder starts a segment when it starts)."""
    return db.execute(f"SELECT id,camera_id,relative_path,starts_at,ends_at,size_bytes FROM recording_segments WHERE {_EPISODE_SEGMENTS} ORDER BY starts_at,id",
                      _episode_range(db, episode)).fetchall()


def _segment_of(db: sqlite3.Connection, episode: sqlite3.Row, segment_id: int) -> sqlite3.Row | None:
    """One segment of a takeover, found by its id and checked to be one of ``_segments_of``: the same work however long the takeover was (#510)."""
    return db.execute(f"SELECT id,camera_id,relative_path,starts_at,ends_at,size_bytes FROM recording_segments WHERE id=? AND {_EPISODE_SEGMENTS}",
                      (segment_id, *_episode_range(db, episode))).fetchone()


def standby_lease(payload: dict[str, Any]) -> dict[str, Any]:
    """The standby's answer to the primary's renewal. Always notes the renewal. While the standby records it stops at once (the primary is back) and says "not yet" until
    its recorders have stopped; then it grants the lease and describes the footage it holds for the primary."""
    now = _now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            settings = read_settings(db)
            if settings["role"] != "standby":
                raise PeerError(409, "This NVR is not a standby")
            state = read_state(db)
            _set_state(db, last_renewed_at=_stamp(now), last_contact_at=_stamp(now))
            lease = settings["lease_seconds"]
            if state["takeover_active"]:
                _set_state(db, takeover_active=0, handback_requested_at=_stamp(now))
                answer = {"granted": False, "state": "stopping", "lease_seconds": lease, "epoch": state["epoch"], "handback": None}
            else:
                # The oldest first: after a second takeover there are two to collect, one a renewal (#498).
                episode = db.execute("SELECT * FROM failover_episodes WHERE role='standby' AND state='handback' ORDER BY epoch LIMIT 1").fetchone()
                if episode is None:
                    answer = {"granted": True, "state": "watching", "lease_seconds": lease, "epoch": state["epoch"], "handback": None}
                else:
                    requested = _parse(state["handback_requested_at"]) or now
                    if not recorders_idle(db) and (now - requested).total_seconds() < STOP_WAIT_SECONDS:
                        answer = {"granted": False, "state": "stopping", "lease_seconds": lease, "epoch": state["epoch"], "handback": None}
                    else:
                        segments = _segments_of(db, episode)
                        answer = {"granted": True, "state": "handback", "lease_seconds": lease, "epoch": state["epoch"],
                                  "handback": {"epoch": episode["epoch"], "takeover_started_at": episode["started_at"], "silent_since": episode["silent_since"], "segments": len(segments)}}
            if answer["state"] == "stopping" and state["takeover_active"]:
                db.execute("UPDATE failover_episodes SET state='handback' WHERE role='standby' AND epoch=?", (state["epoch"],))
                _takeover_incident(db, "standby", True, {"epoch": state["epoch"]}, waiting=True)  # It is no longer recording: the text says so
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return answer


def standby_footage(epoch: int) -> list[dict[str, Any]]:
    with connect() as db:
        episode = db.execute("SELECT * FROM failover_episodes WHERE role='standby' AND epoch=?", (epoch,)).fetchone()
        if episode is None:
            raise PeerError(404, "No such takeover")
        return [dict(row) for row in _segments_of(db, episode)]


def standby_footage_file(epoch: int, segment_id: int) -> tuple[Path, str]:
    """The file of one segment recorded during the takeover, and its SHA-256."""
    from .footage_chain import file_sha256
    from .storage import recording_file_path

    with connect() as db:
        episode = db.execute("SELECT * FROM failover_episodes WHERE role='standby' AND epoch=?", (epoch,)).fetchone()
        if episode is None:
            raise PeerError(404, "No such takeover")
        row = _segment_of(db, episode, segment_id)
    if row is None:
        raise PeerError(404, "No such footage in that takeover")
    try:
        path = recording_file_path(row["relative_path"])
    except (OSError, ValueError):
        raise PeerError(404, "The footage is not available") from None
    if not path.is_file():
        raise PeerError(404, "The footage is not available")
    return path, file_sha256(path)


def standby_handback_complete(epoch: int, imported: int, imported_bytes: int) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if read_settings(db)["role"] != "standby":
                raise PeerError(409, "This NVR is not a standby")
            episode = db.execute("SELECT * FROM failover_episodes WHERE role='standby' AND epoch=?", (epoch,)).fetchone()
            if episode is None:
                raise PeerError(404, "No such takeover")
            if episode["state"] == "active":
                raise PeerError(409, "The takeover is still going on: the standby has not been asked to stop")
            if episode["state"] != "done":
                db.execute("UPDATE failover_episodes SET state='done',ended_at=?,segments_imported=?,bytes_imported=? WHERE id=?", (_stamp(), imported, imported_bytes, episode["id"]))
                more = db.execute("SELECT 1 FROM failover_episodes WHERE role='standby' AND state='handback' LIMIT 1").fetchone() is not None
                if more:  # An earlier or later takeover's footage is still to be collected
                    _takeover_incident(db, "standby", True, {"epoch": epoch, "segments_handed_back": imported}, waiting=True)
                else:
                    _takeover_incident(db, "standby", False, {"epoch": epoch, "segments_handed_back": imported})
                _set_state(db, handback_requested_at=None if not more else read_state(db)["handback_requested_at"], watching_since=_stamp(), last_error=None)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def take_over(db: sqlite3.Connection, silent_since: datetime | None, now: datetime | None = None) -> int:
    """The standby starts recording in the primary's place, inside the caller's transaction; returns the new epoch."""
    now = now or _now()
    epoch = int(read_state(db)["epoch"]) + 1
    db.execute("INSERT INTO failover_episodes(id,epoch,role,silent_since,started_at,state) VALUES(?,?,'standby',?,?,'active')", (str(uuid.uuid4()), epoch, _stamp(silent_since) if silent_since else None, _stamp(now)))
    _set_state(db, takeover_active=1, takeover_started_at=_stamp(now), epoch=epoch, last_error=None)
    _takeover_incident(db, "standby", True, {"epoch": epoch, "silent_since": _stamp(silent_since) if silent_since else None})
    return epoch


def check_takeover(now: datetime | None = None) -> int | None:
    """Take over if the primary has been silent for ``silence_seconds`` (counted from its last renewal, or from when the standby began watching) and a configuration
    has been copied to record from. Returns the epoch when it did."""
    now = now or _now()
    with connect() as db:
        # Read first: this runs every two seconds, and a standby whose primary is alive has nothing to change (#280, #353). What is read here is
        # read again under the lock before anything is done.
        settings, state = read_settings(db), read_state(db)
        if settings["role"] != "standby" or state["takeover_active"]:
            return None
        if state["watching_since"]:
            last_heard = _parse(state["last_renewed_at"]) or _parse(state["watching_since"])
            if (now - last_heard).total_seconds() < settings["silence_seconds"]:
                return None
        db.execute("BEGIN IMMEDIATE")
        try:
            settings, state = read_settings(db), read_state(db)
            # A standby that has stopped for a primary that came back is waiting for a primary that is alive. If that primary goes silent
            # again before it has collected the footage, the standby records again: it used to wait for ever, with neither NVR
            # recording and an incident that said this one was (#498). The earlier takeover's footage stays on offer.
            if settings["role"] != "standby" or state["takeover_active"]:
                db.execute("ROLLBACK")
                return None
            if not state["watching_since"]:
                _set_state(db, watching_since=_stamp(now))
                db.execute("COMMIT")
                return None
            since = _parse(state["last_renewed_at"]) or _parse(state["watching_since"])
            if (now - since).total_seconds() < settings["silence_seconds"]:
                db.execute("ROLLBACK")
                return None
            if not state["config_created_at"] and db.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 0:
                _set_state(db, last_error="The primary is silent but this standby has no cameras to record: no configuration has been copied from it yet")
                db.execute("COMMIT")
                return None
            epoch = take_over(db, since, now)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return epoch


# --- copying the primary's configuration -----------------------------------------------------------------------------------------------------------

class SyncError(RuntimeError):
    pass


class NoRoom(OSError):
    """Bringing back a segment would take the recordings volume below its headroom."""


class MissingFootage(Exception):
    """The standby no longer has a segment it listed (its retention removed it): it is skipped, not waited for."""


WORK_MARGIN_BYTES = 16 << 20  # Left free beside the database after a sync's working files


def apply_configuration(archive: Path) -> dict[str, Any]:
    """Copy the configuration needed to record from what the primary sent into this standby.

    Refused unless it is the primary's (its credential key is this standby's own), it is not from a newer version, and it passes its
    integrity check. It is decrypted as it is read, into a folder beside the database: the only unencrypted copies are in the protected
    data folder, as for a backup (#328). It used to be unpacked, three times over, in the system's temporary folder (#501)."""
    secret = backup.passphrase()
    if secret is None:
        raise SyncError("Set NVR_BACKUP_PASSPHRASE to the passphrase the primary's backups use")
    data = database_path().parent
    needed = archive.stat().st_size + WORK_MARGIN_BYTES
    free = shutil.disk_usage(data).free
    if free < needed:
        raise SyncError(f"There is not enough room to copy the primary's configuration: {free // (1 << 20)} MB free where {data} is, {needed // (1 << 20)} MB needed")
    with tempfile.TemporaryDirectory(prefix=WORK_PREFIX, dir=data) as work:
        work_dir = Path(work)
        os.chmod(work_dir, 0o700)
        try:
            manifest = backup._unpack_checked(archive, secret, work_dir)
        except backup.BackupError as exc:
            raise SyncError(f"The primary's configuration could not be opened: {exc}") from None
        if not key_path().is_file() or (work_dir / "credentials.key").read_bytes() != key_path().read_bytes():
            raise SyncError("The backup is not from the primary this standby was set up from: its credential key differs. Restore a backup of the primary on this standby once "
                            "(python -m app.backup restore) before turning failover on")
        return _mirror(work_dir / "nvr.sqlite3", manifest)


WORK_PREFIX = ".failover-"


def remove_leftovers() -> int:
    """At start: working folders a sync left when its process was killed. They hold a copy of the configuration and the credential key."""
    removed = 0
    try:
        for leftover in database_path().parent.glob(f"{WORK_PREFIX}*"):
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
                removed += 1
    except OSError:
        pass
    return removed


def _columns(db: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [row[1] for row in db.execute(f"PRAGMA {schema}.table_info({table})")]


def _mirror(source: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    db = connect()
    try:
        local_version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if manifest.get("schema_version") and local_version is not None and int(manifest["schema_version"]) > int(local_version):
            raise SyncError("The primary runs a newer version than this standby: update the standby first")
        db.execute("ATTACH DATABASE ? AS src", (str(source),))
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                counts = _copy(db)
                now = _stamp()
                _set_state(db, last_sync_at=now, last_sync_error=None, config_created_at=manifest.get("created_at"))
                sync_incident(db, incident_key="failover:sync", camera_id=None, fault_type="failover_sync", summary="", active=False)
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.execute("DETACH DATABASE src")
    finally:
        db.close()
    return counts


def _copy(db: sqlite3.Connection) -> dict[str, int]:
    volumes = {row[0] for row in db.execute("SELECT id FROM storage_volumes")} | {"default"}
    counts: dict[str, int] = {}
    # Groups first (cameras refer to them); a storage volume this standby does not have means its own default.
    wanted = {row["id"] for row in db.execute("SELECT id FROM src.camera_groups")}
    for row in db.execute("SELECT * FROM src.camera_groups").fetchall():
        volume = row["storage_volume_id"] if row["storage_volume_id"] in volumes else None
        db.execute("INSERT INTO camera_groups(id,name,storage_volume_id,created_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,storage_volume_id=excluded.storage_volume_id",
                   (row["id"], row["name"], volume, row["created_at"]))
    for (group_id,) in db.execute("SELECT id FROM camera_groups").fetchall():
        if group_id not in wanted:
            db.execute("DELETE FROM camera_groups WHERE id=?", (group_id,))
    counts["groups"] = len(wanted)
    columns = [name for name in CAMERA_COLUMNS if name in _columns(db, "src", "cameras") and name in _columns(db, "main", "cameras")]
    names = ",".join(columns)
    updates = ",".join(f"{name}=excluded.{name}" for name in columns)
    cameras = {row["id"] for row in db.execute("SELECT id FROM src.cameras")}
    db.execute(f"INSERT INTO cameras(id,{names}) SELECT id,{names} FROM src.cameras WHERE true ON CONFLICT(id) DO UPDATE SET {updates}")
    for (camera_id,) in db.execute("SELECT id FROM cameras").fetchall():
        if camera_id in cameras:
            continue
        # A camera removed at the primary: its footage here is kept, so it is switched off and archived rather than deleted.
        if db.execute("SELECT 1 FROM recording_segments WHERE camera_id=? LIMIT 1", (camera_id,)).fetchone():
            db.execute("UPDATE cameras SET enabled=0,archived_at=COALESCE(archived_at,?) WHERE id=?", (_stamp(), camera_id))
        else:
            db.execute("DELETE FROM cameras WHERE id=?", (camera_id,))
    counts["cameras"] = len(cameras)
    for table in REPLACED_TABLES:
        shared = [name for name in _columns(db, "src", table) if name in _columns(db, "main", table)]
        db.execute(f"DELETE FROM {table}")
        joined = ",".join(shared)
        extra = ""
        if table == "camera_group_memberships":
            extra = " WHERE camera_id IN (SELECT id FROM cameras) AND group_id IN (SELECT id FROM camera_groups)"
        elif table == "privacy_masks":
            extra = " WHERE camera_id IN (SELECT id FROM cameras)"
        db.execute(f"INSERT INTO {table}({joined}) SELECT {joined} FROM src.{table}{extra}")
        counts[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    display = db.execute("SELECT * FROM src.site_display_settings WHERE singleton_id=1").fetchone()
    if display is not None:
        db.execute("INSERT INTO site_display_settings(singleton_id,time_zone,date_format,clock_format,updated_at) VALUES(1,?,?,?,?) ON CONFLICT(singleton_id) DO UPDATE SET "
                   "time_zone=excluded.time_zone,date_format=excluded.date_format,clock_format=excluded.clock_format,updated_at=excluded.updated_at",
                   (display["time_zone"], display["date_format"], display["clock_format"], display["updated_at"]))
    return counts


# --- the primary's side: serving its backup, importing footage ----------------------------------------------------------------------------------------

# What the standby copies (see ``_copy``), and the schema's version so that it can refuse a primary newer than itself.
EXPORT_TABLES = ("camera_groups", "cameras", *REPLACED_TABLES, "site_display_settings", "schema_migrations")
EXPORT_REUSE_SECONDS = 60.0  # A standby that asks again within this long is given the export just made
_export_lock = threading.Lock()


def _export_configuration(target: Path) -> None:
    """A small database holding only the tables the standby copies, read from the live one."""
    export = sqlite3.connect(target, isolation_level=None)
    try:
        export.execute("ATTACH DATABASE ? AS live", (str(database_path()),))
        export.execute("BEGIN")
        present = {row[0] for row in export.execute("SELECT name FROM live.sqlite_master WHERE type='table'")}
        for table in EXPORT_TABLES:
            if table in present:
                export.execute(f"CREATE TABLE {table} AS SELECT * FROM live.{table}")
        export.execute("COMMIT")
        export.execute("DETACH DATABASE live")
    finally:
        export.close()


def backup_for_standby() -> Path:
    """The primary's configuration for the standby, sealed as a backup is (it holds the credential key): a small export of the tables the
    standby copies, made now unless one was made in the last minute.

    This used to be a backup of the whole database, made for every sync to move a few small tables: each sync wrote the database about
    twice over on the primary and three times on the standby, and past some size no sync finished within the standby's wait (#501). Two
    requests at once now make one export."""
    if backup.disabled_reason():
        raise PeerError(409, backup.disabled_reason() or "Backups are not configured")
    with connect() as db:
        if read_settings(db)["role"] != "primary":
            raise PeerError(409, "This NVR is not a primary")
    directory = backup.destination()
    with _export_lock:
        made = sorted(directory.glob(f"*-failover{backup.SUFFIX}"), key=lambda path: path.stat().st_mtime) if directory.is_dir() else []
        if made and time.time() - made[-1].stat().st_mtime <= EXPORT_REUSE_SECONDS:
            return made[-1]
        try:
            with tempfile.TemporaryDirectory(prefix=f"{WORK_PREFIX}export-", dir=database_path().parent) as work:
                os.chmod(work, 0o700)
                export = Path(work) / "configuration.sqlite3"
                _export_configuration(export)
                result = backup.create_backup("failover", database=export)
        except backup.BackupError as exc:
            raise PeerError(409, str(exc)) from None
        return directory / str(result["file_name"])


def _episode(db: sqlite3.Connection, role: str, epoch: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM failover_episodes WHERE role=? AND epoch=?", (role, epoch)).fetchone()


def learn_of_takeover(handback: dict[str, Any]) -> None:
    """The primary learns the standby recorded while it was away: an episode and an incident, until the footage is back."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if _episode(db, "primary", int(handback["epoch"])) is None:
                db.execute("INSERT INTO failover_episodes(id,epoch,role,silent_since,started_at,state) VALUES(?,?,'primary',?,?,'handback')",
                           (str(uuid.uuid4()), int(handback["epoch"]), handback.get("silent_since"), handback["takeover_started_at"]))
                _takeover_incident(db, "primary", True, {"epoch": int(handback["epoch"]), "takeover_started_at": handback["takeover_started_at"], "segments": handback.get("segments")})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def store_imported(epoch: int, remote: dict[str, Any], path: Path, digest: str) -> bool:
    """Index one segment recorded by the standby (``source='failover'``) once its file is in place; False when it is not one of this NVR's cameras."""
    from .recording import recordings_path

    relative = path.resolve().relative_to(recordings_path().resolve()).as_posix()
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (remote["camera_id"],)).fetchone() is None:
            return False
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes,source) VALUES(?,?,?,?,?,'failover') ON CONFLICT(relative_path) DO NOTHING",
                       (remote["camera_id"], relative, remote["starts_at"], remote["ends_at"], path.stat().st_size))  # The size of the file that is here, not the one the standby said
            segment = db.execute("SELECT id FROM recording_segments WHERE relative_path=?", (relative,)).fetchone()
            db.execute("INSERT OR IGNORE INTO failover_imports(epoch,remote_segment_id,segment_id,camera_id,sha256) VALUES(?,?,?,?,?)", (epoch, remote["id"], segment["id"], remote["camera_id"], digest))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return True


def finish_handback(epoch: int, imported: int, imported_bytes: int) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            episode = _episode(db, "primary", epoch)
            if episode is not None and episode["state"] != "done":
                db.execute("UPDATE failover_episodes SET state='done',ended_at=?,segments_imported=?,bytes_imported=? WHERE id=?", (_stamp(), imported, imported_bytes, episode["id"]))
                _takeover_incident(db, "primary", False, {"epoch": epoch, "segments_imported": imported})
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


# --- the administrator's and the status views -----------------------------------------------------------------------------------------------------

def status_view(db: sqlite3.Connection) -> dict[str, Any]:
    settings, state = read_settings(db), read_state(db)
    role = settings["role"]
    held = held_reason(db)
    if role == "off":
        headline = "Failover is off"
    elif role == "standby":
        if state["takeover_active"]:
            headline = "Taken over: this standby is recording in the primary's place"
        elif db.execute("SELECT 1 FROM failover_episodes WHERE role='standby' AND state='handback'").fetchone():
            headline = "Handing back to the primary"
        else:
            headline = "Standby: watching the primary"
    else:
        headline = {"ok": "Primary: the standby is watching", "unreachable": "Primary: the standby cannot be reached (recording continues)", "refused": "Primary: the standby refused the request (see the last error)",
                    "no_room": "Primary: not enough free space to bring back the standby's footage",
                    "starting": "Primary: starting, waiting to hear from the standby", "standby_stopping": "Primary: waiting for the standby to stop recording", "handback": "Primary: bringing back the standby's footage",
                    "unverified": "Primary: the standby's answer was not signed and was ignored (recording continues)",
                    "wrong_certificate": "Primary: the standby presented a certificate other than the one it was paired with, and was refused (recording continues)",
                    "not_handing_back": "Primary: the standby did not hand back in time; this primary is recording again",
                    }.get(str(state["peer_state"]), "Primary")
    return {"role": role, "headline": headline, "lease_until": state["lease_until"], "recording_held": held, "peer_state": state["peer_state"], "last_contact_at": state["last_contact_at"],
            "takeover_active": bool(state["takeover_active"]), "takeover_started_at": state["takeover_started_at"], "epoch": state["epoch"], "last_error": state["last_error"],
            "last_sync_at": state["last_sync_at"], "last_sync_error": state["last_sync_error"], "config_created_at": state["config_created_at"],
            "link": None if role == "off" or not settings["peer_url"] else "unencrypted" if str(settings["peer_url"]).lower().startswith("http://")
            else "pinned" if settings["peer_fingerprint"] else "https"}


# --- the manager --------------------------------------------------------------------------------------------------------------------------------

class FailoverManager:
    """Runs this NVR's side: the primary renews its lease and brings the standby's footage back; the standby watches, copies the configuration and takes over."""

    def __init__(self, tick_seconds: float = TICK_SECONDS) -> None:
        self.tick_seconds = tick_seconds
        self.task: asyncio.Task[None] | None = None
        self._last_renewal = float("-inf")
        self._last_sync = float("-inf")

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="failover")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed pass is tried again at the next
                logger.exception("A failover pass failed")
            await asyncio.sleep(self.tick_seconds)

    async def tick(self) -> None:
        with connect() as db:
            settings = read_settings(db)
            secret = shared_secret(db)
        if settings["role"] == "off" or not settings["peer_url"] or not secret:
            return
        if settings["role"] == "primary":
            await self._primary(settings, secret)
        else:
            await self._standby(settings, secret)

    # The primary

    def _renewal_due(self, settings: dict[str, Any], held: bool) -> bool:
        interval = POLL_WHILE_HELD_SECONDS if held else max(3.0, settings["lease_seconds"] / 4)
        return time.monotonic() - self._last_renewal >= interval

    async def _primary(self, settings: dict[str, Any], secret: str) -> None:
        with connect() as db:
            held = held_reason(db) is not None
            pending = db.execute("SELECT epoch FROM failover_episodes WHERE role='primary' AND state='handback' ORDER BY epoch LIMIT 1").fetchone()
        if self._renewal_due(settings, held):
            self._last_renewal = time.monotonic()
            await self._renew(settings, secret)
            with connect() as db:
                pending = db.execute("SELECT epoch FROM failover_episodes WHERE role='primary' AND state='handback' ORDER BY epoch LIMIT 1").fetchone()
        if pending is not None and not held_now():
            await self._import(settings, secret, int(pending["epoch"]))

    async def _call(self, settings: dict[str, Any], secret: str, method: str, path: str, payload: dict[str, Any] | None = None, *,
                    client: httpx.AsyncClient | None = None, **options: Any) -> httpx.Response:
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
        headers = {**sign(secret, method, path, body), "Content-Type": "application/json"}
        url = check_peer_url(settings["peer_url"], settings["allow_private"]) + path
        if client is not None:  # An import's own connection, kept open across its requests (#510)
            response = await client.request(method, url, content=body or None, headers=headers, timeout=TIMEOUT, **options)
        else:
            async with link_client(settings, TIMEOUT) as fresh:
                response = await fresh.request(method, url, content=body or None, headers=headers, **options)
        response.extensions["failover_request_signature"] = headers["X-Failover-Signature"]  # What a signed answer must be an answer to
        return response

    async def _renew(self, settings: dict[str, Any], secret: str) -> None:
        sent = _now()
        try:
            response = await self._call(settings, secret, "POST", "/api/inbound/failover/lease", {"instance_id": settings["instance_id"], "lease_seconds": settings["lease_seconds"]})
        except CertificateMismatch as exc:  # Refused before anything was sent: the primary keeps recording, and says why (#502)
            await asyncio.to_thread(note_certificate, exc)
            return
        except LinkRefused as exc:
            await asyncio.to_thread(self._record_peer, "refused", str(exc))
            return
        except (httpx.HTTPError, ValueError) as exc:
            await asyncio.to_thread(self._record_peer, "unreachable", f"The standby could not be reached: {type(exc).__name__}")
            return
        if response.status_code in (401, 403, 426):
            await asyncio.to_thread(self._record_peer, "refused", self._refusal_reason(response))
            return
        if response.status_code != 200:
            await asyncio.to_thread(self._record_peer, "error", f"The standby answered {response.status_code}")
            return
        # Only an answer the standby signed, for this request, counts. Anything else is no answer: the primary neither yields nor
        # takes its lease as renewed on the word of whoever could reach it (#343).
        problem = self._answer_problem(secret, "/api/inbound/failover/lease", response)
        if problem:
            await asyncio.to_thread(self._record_peer, "unverified", self._unverified_message(problem))
            return
        try:
            answer = response.json()
        except ValueError:
            await asyncio.to_thread(self._record_peer, "error", "The standby's answer was not understood")
            return
        await asyncio.to_thread(self._apply_answer, settings, sent, answer)

    @staticmethod
    def _answer_problem(secret: str, path: str, response: httpx.Response, body: bytes | None = None) -> str | None:
        """Why the standby's answer to this request is not to be believed, or None. ``body`` is what was signed when it is not the response's
        content (a footage file's digest and size)."""
        return answer_problem(secret, path, response.content if body is None else body, str(response.extensions.get("failover_request_signature", "")),
                              response.headers.get("x-failover-timestamp"), response.headers.get("x-failover-signature"))

    @staticmethod
    def _unverified_message(problem: str) -> str:
        if problem.startswith("clock:"):
            return clock_message(problem, "The standby's answer")
        return "The standby's answer was not signed with the failover secret and was ignored; both NVRs must run the same version, with the same secret"

    @staticmethod
    def _refusal_reason(response: httpx.Response) -> str:
        """What the standby said when it refused a request: a clock difference or an NVR not yet upgraded is named; anything else is the secrets."""
        try:
            detail = str(response.json().get("detail", ""))
        except (ValueError, AttributeError):
            detail = ""
        if "clocks differ" in detail or "upgrade both" in detail or "unencrypted" in detail:
            return f"The standby refused the request: {detail[:300]}"
        return "The standby refused the signature: the failover secrets differ"

    @staticmethod
    def _record_peer(peer_state: str, error: str | None) -> None:
        with connect() as db:
            _set_state(db, peer_state=peer_state, last_error=error)

    @staticmethod
    def _apply_answer(settings: dict[str, Any], sent: datetime, answer: dict[str, Any]) -> None:
        with connect() as db:
            certificate_incident(db, False)  # A verified answer: the link is the paired one again (read first: nothing is written while none is active)
            if answer.get("granted") is True:
                _set_state(db, lease_until=_stamp(sent + timedelta(seconds=settings["lease_seconds"])), yield_until=None, yield_since=None, last_contact_at=_stamp(), last_error=None,
                           peer_state="handback" if answer.get("handback") else "ok", epoch=int(answer.get("epoch") or 0))
                _yield_incident(db, active=False)
            else:
                now = _now()
                since = _parse(read_state(db).get("yield_since")) or now
                if (now - since).total_seconds() >= YIELD_LIMIT_SECONDS:
                    # Still "not yet" after the limit: this primary records again and says so, rather than record nothing for as long as it is told to wait.
                    _set_state(db, yield_until=None, yield_since=_stamp(since), last_contact_at=_stamp(), peer_state="not_handing_back",
                               last_error="The standby has kept this primary waiting too long; this primary is recording again")
                    _yield_incident(db, active=True, since=_stamp(since))
                else:
                    _set_state(db, yield_until=_stamp(now + timedelta(seconds=settings["lease_seconds"])), yield_since=_stamp(since), last_contact_at=_stamp(), last_error=None,
                               peer_state="standby_stopping")
        if answer.get("granted") is True and isinstance(answer.get("handback"), dict):
            learn_of_takeover(answer["handback"])

    async def _import(self, settings: dict[str, Any], secret: str, epoch: int) -> None:
        """Bring back the footage the standby recorded: each segment once, checked against the digest the standby gives, then tell it so it can recover.

        One connection carries the whole import, and the lease is renewed between files as it falls due: a long takeover's footage takes
        a while, and a primary that stopped renewing for all of it looked silent to its standby (#510)."""
        if self._last_renewal == float("-inf"):
            self._last_renewal = time.monotonic()  # The pass that leads here renews first; counted from the import's start otherwise
        try:
            client = link_client(settings, httpx.Timeout(120.0, connect=4.0))
        except LinkRefused:
            return  # The renewal says why
        async with client:
            await self._import_over(client, settings, secret, epoch)

    async def _keep_lease(self, settings: dict[str, Any], secret: str) -> None:
        if self._renewal_due(settings, held=False):
            self._last_renewal = time.monotonic()
            await self._renew(settings, secret)

    async def _import_over(self, client: httpx.AsyncClient, settings: dict[str, Any], secret: str, epoch: int) -> None:
        from .storage import camera_recordings_dir

        path = f"/api/inbound/failover/footage?epoch={epoch}"
        try:
            listing = await self._call(settings, secret, "GET", path, client=client)
            if listing.status_code != 200:
                return
            # Only a list the standby signed, for this request, is used: one added to or cut on the way is no list, and is asked for again (#502).
            problem = self._answer_problem(secret, path, listing)
            if problem:
                await asyncio.to_thread(self._record_peer, "unverified", self._unverified_message(problem))
                return
            remote = listing.json().get("segments", [])
        except (httpx.HTTPError, ValueError):
            return
        with connect() as db:
            done = {row[0] for row in db.execute("SELECT remote_segment_id FROM failover_imports WHERE epoch=?", (epoch,))}
        for raw in remote:
            try:
                started, ended = _parse(raw["starts_at"]), _parse(raw["ends_at"])
                if started is None or ended is None or not 0 < (ended - started).total_seconds() <= MAX_SEGMENT_SECONDS:
                    continue
                # What the standby says is cast and rewritten before it is used: the path is made from these, never from the text it sent.
                item = {"id": int(raw["id"]), "camera_id": str(raw["camera_id"]), "starts_at": started.astimezone(timezone.utc).isoformat(), "ends_at": ended.astimezone(timezone.utc).isoformat(),
                        "size_bytes": int(raw["size_bytes"])}
                if item["id"] in done:
                    continue
                directory = camera_recordings_dir(item["camera_id"]) if await asyncio.to_thread(self._has_camera, item["camera_id"]) else None
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if directory is None:
                continue
            target = directory / f"failover-e{epoch}-{item['id']}-{started:%Y%m%dT%H%M%SZ}.mp4"
            await self._keep_lease(settings, secret)
            try:
                digest = await self._download(settings, secret, epoch, int(item["id"]), target, item["size_bytes"], client=client)
            except NoRoom as exc:
                await asyncio.to_thread(self._record_peer, "no_room", str(exc))
                return  # Tried again at the next pass, when retention may have made room
            except MissingFootage:
                logger.warning("The standby no longer has footage %s of takeover %s: skipped", item["id"], epoch)
                await asyncio.to_thread(self._skip, epoch, item)
                continue
            except (httpx.HTTPError, OSError, ValueError):
                return  # Tried again at the next pass
            try:
                await asyncio.to_thread(store_imported, epoch, item, target, digest)
            except (OSError, ValueError):
                target.unlink(missing_ok=True)  # Not indexable here: left out rather than retried for ever
                await asyncio.to_thread(self._skip, epoch, item)
        with connect() as db:
            totals = db.execute("SELECT COUNT(i.segment_id),COALESCE(SUM(s.size_bytes),0) FROM failover_imports i LEFT JOIN recording_segments s ON s.id=i.segment_id WHERE i.epoch=?", (epoch,)).fetchone()
        try:
            confirmed = await self._call(settings, secret, "POST", "/api/inbound/failover/handback-complete", {"epoch": epoch, "imported": totals[0], "bytes": totals[1]}, client=client)
        except (httpx.HTTPError, ValueError):
            return
        # The handback is done only when the standby says so in a signed answer: an unsigned "done" would leave the standby waiting (#502).
        if confirmed.status_code == 200 and not self._answer_problem(secret, "/api/inbound/failover/handback-complete", confirmed):
            await asyncio.to_thread(finish_handback, epoch, int(totals[0]), int(totals[1]))

    @staticmethod
    def _skip(epoch: int, item: dict[str, Any]) -> None:
        with connect() as db:
            db.execute("INSERT OR IGNORE INTO failover_imports(epoch,remote_segment_id,segment_id,camera_id,sha256) VALUES(?,?,NULL,?,'')", (epoch, item["id"], item["camera_id"]))

    @staticmethod
    def _has_camera(camera_id: str) -> bool:
        with connect() as db:
            return db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is not None

    async def _download(self, settings: dict[str, Any], secret: str, epoch: int, segment_id: int, target: Path, listed_size: int, *,
                        client: httpx.AsyncClient | None = None) -> str:
        """One segment, believed only as the standby signed it: its digest and size, signed as the answer to this request, the size the one it
        listed, and the bytes checked against both as they arrive (#502). Nothing is written that would take the recordings volume below its
        headroom, and "no such footage" counts only when signed."""
        from .exports import free_floor

        path = f"/api/inbound/failover/footage/{segment_id}?epoch={epoch}"
        headers = sign(secret, "GET", path)
        url = check_peer_url(settings["peer_url"], settings["allow_private"]) + path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not 0 <= listed_size <= MAX_FOOTAGE_BYTES:
            raise ValueError("The standby listed a segment larger than any recording")
        if await asyncio.to_thread(lambda: shutil.disk_usage(target.parent).free - listed_size < free_floor(target.parent)):
            raise NoRoom("There is not enough free space on the recordings volume to bring back the standby's footage without the recorder deleting footage")
        partial = target.with_name(f".{target.name}.partial")
        digest, received = hashlib.sha256(), 0
        own = client is None
        if own:
            client = link_client(settings, httpx.Timeout(120.0, connect=4.0))
        try:
            async with contextlib.AsyncExitStack() as stack:
                if own:
                    await stack.enter_async_context(client)
                async with client.stream("GET", url, headers=headers) as response:
                    response.extensions["failover_request_signature"] = headers["X-Failover-Signature"]
                    if response.status_code == 404:
                        await response.aread()
                        if self._answer_problem(secret, path, response) is None:
                            raise MissingFootage
                        raise ValueError("An unsigned 'no such footage' was ignored")
                    if response.status_code != 200:
                        raise httpx.HTTPError(f"The standby answered {response.status_code}")
                    expected = response.headers.get("X-Footage-Sha256", "")
                    try:
                        size = int(response.headers.get("X-Footage-Size", ""))
                    except ValueError:
                        raise ValueError("The standby did not say how large the footage is") from None
                    if self._answer_problem(secret, path, response, footage_summary(expected, size)):
                        raise ValueError("The footage's digest and size were not signed by the standby")
                    if size != listed_size:
                        raise ValueError("The footage is not the size the standby listed")
                    with partial.open("wb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            received += len(chunk)
                            if received > size:
                                raise ValueError("The standby sent more than the footage's signed size")
                            digest.update(chunk)
                            await asyncio.to_thread(handle.write, chunk)
            if received != size or not expected or digest.hexdigest() != expected:
                raise ValueError("The footage did not arrive intact")
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return digest.hexdigest()

    # The standby

    async def _standby(self, settings: dict[str, Any], secret: str) -> None:
        await asyncio.to_thread(check_takeover)
        with connect() as db:
            state = read_state(db)
        due = bool(state["sync_requested"]) or time.monotonic() - self._last_sync >= settings["sync_seconds"]
        if due and not state["takeover_active"]:
            await asyncio.to_thread(self._clear_sync_request)
            self._last_sync = time.monotonic()
            await self.sync(settings, secret)

    @staticmethod
    def _clear_sync_request() -> None:
        with connect() as db:
            _set_state(db, sync_requested=0)

    async def sync(self, settings: dict[str, Any], secret: str) -> dict[str, Any] | None:
        """Fetch the primary's backup and copy its configuration; a failure is kept and shown, and raises an incident once it has gone on for two intervals."""
        try:
            path = "/api/inbound/failover/backup"
            headers = sign(secret, "GET", path)
            url = check_peer_url(settings["peer_url"], settings["allow_private"]) + path
            with tempfile.TemporaryDirectory(prefix=f"{WORK_PREFIX}fetch-", dir=database_path().parent) as directory:  # Beside the database, not in the system's temporary folder
                archive = Path(directory) / "primary.nvrbackup"
                async with link_client(settings, httpx.Timeout(300.0, connect=4.0)) as client:
                    async with client.stream("GET", url, headers=headers) as response:
                        if response.status_code != 200:
                            detail = (await response.aread()).decode("utf-8", "replace")[:200]
                            raise SyncError(f"The primary answered {response.status_code}: {detail}")
                        with archive.open("wb") as handle:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                await asyncio.to_thread(handle.write, chunk)
                copied = await asyncio.to_thread(apply_configuration, archive)
            if settings.get("peer_fingerprint"):
                await asyncio.to_thread(note_certificate, None)
            return copied
        except CertificateMismatch as exc:
            await asyncio.to_thread(note_certificate, exc)
            await asyncio.to_thread(self._sync_failed, settings, mismatch_message(exc))
            return None
        except (httpx.HTTPError, ValueError, SyncError, OSError, tarfile.TarError, sqlite3.Error) as exc:
            message = str(exc) if isinstance(exc, (SyncError, LinkRefused)) else f"The primary could not be reached for its configuration: {type(exc).__name__}"
            await asyncio.to_thread(self._sync_failed, settings, message)
            return None

    @staticmethod
    def _sync_failed(settings: dict[str, Any], message: str) -> None:
        with connect() as db:
            state = read_state(db)
            _set_state(db, last_sync_error=message)
            last = _parse(state["last_sync_at"]) or _parse(state["watching_since"]) or _now()
            sync_incident(db, incident_key="failover:sync", camera_id=None, fault_type="failover_sync",
                          summary=f"The standby cannot copy the primary's configuration: {message}", active=(_now() - last).total_seconds() > 2 * settings["sync_seconds"], details={"error": message})


def held_now() -> bool:
    with connect() as db:
        return held_reason(db) is not None
