"""Web Push notifications for alarms.

Each person subscribes the devices they want notified and chooses which rules or
camera groups notify them, with optional quiet hours. When a rule raises an alarm,
a delivery is queued for every subscribed device whose owner may see the camera and
wants this alarm now. The background worker sends them: payloads are encrypted for
the device (RFC 8291, ``aes128gcm``) and signed for the push service with the
installation's VAPID key (RFC 8292). Failures retry with backoff; a subscription the
push service reports gone (404/410) is removed.

A notification's thumbnail is fetched through a short-lived signed link that names
one alarm. It carries no session or API token.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import struct
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import base64url
from .access import may_view_camera
from .alarms import label_text
from .credentials import decrypt, derived_key, encrypt
from .store import connect, has_row
from app.clock import now as _now
from .audit import record as audit_record

logger = logging.getLogger("nvr.push")

# Long enough to outlive the message's time to live and every retry.
THUMBNAIL_LIFETIME = timedelta(hours=2)
KEEP_DELIVERIES = timedelta(days=7)
TTL_SECONDS = 3600
MAX_ATTEMPTS = 6
RETRY_SECONDS = (10, 30, 120, 600, 1800)
RECORD_SIZE = 4096
# Push services accept at most 4096 bytes of body: an 86-byte header (salt, record size, key),
# the 16-byte authentication tag and the padding delimiter leave this much for the message.
MAX_PAYLOAD_BYTES = 4096 - 86 - 16 - 1


class PushError(Exception):
    pass


_b64, _unb64 = base64url.encode, base64url.decode


# --- keys -------------------------------------------------------------------------

def _keys(db: sqlite3.Connection) -> tuple[ec.EllipticCurvePrivateKey, str]:
    row = db.execute("SELECT private_key_ciphertext,public_key FROM push_keys WHERE singleton_id=1").fetchone()
    if row is None:
        private = ec.generate_private_key(ec.SECP256R1())
        pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode("ascii")
        public = _b64(private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        db.execute("INSERT OR IGNORE INTO push_keys(singleton_id,private_key_ciphertext,public_key) VALUES(1,?,?)", (encrypt(pem), public))
        row = db.execute("SELECT private_key_ciphertext,public_key FROM push_keys WHERE singleton_id=1").fetchone()
    private = serialization.load_pem_private_key(decrypt(row["private_key_ciphertext"]).encode("ascii"), password=None)
    assert isinstance(private, ec.EllipticCurvePrivateKey)
    return private, row["public_key"]


def public_key() -> str:
    """The VAPID application server key browsers subscribe with (base64url, uncompressed P-256)."""
    with connect() as db:
        return _keys(db)[1]


def vapid_subject() -> str:
    configured = os.environ.get("NVR_VAPID_SUBJECT", "").strip()
    if configured:
        return configured
    public_url = os.environ.get("NVR_PUBLIC_URL", "").strip()
    return public_url if public_url.startswith("https://") else "mailto:nvr-administrator@example.invalid"


def vapid_header(endpoint: str, private: ec.EllipticCurvePrivateKey, public: str, now: datetime | None = None) -> str:
    parts = urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(((now or _now()) + timedelta(hours=12)).timestamp()), "sub": vapid_subject()}
    signing_input = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." + _b64(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(private.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256())))
    token = signing_input + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token}, k={public}"


def encrypt_payload(payload: bytes, p256dh: str, auth: str, *, sender: ec.EllipticCurvePrivateKey | None = None, salt: bytes | None = None) -> bytes:
    """Encrypt one push message for a subscription (RFC 8291 with RFC 8188 ``aes128gcm``).

    ``sender`` and ``salt`` are fresh for every message; they are parameters only so the RFC's
    worked example can be reproduced exactly.
    """
    receiver = _unb64(p256dh)
    secret = _unb64(auth)
    if len(receiver) != 65 or len(secret) != 16:
        raise PushError("The subscription keys are malformed")
    ephemeral = sender or ec.generate_private_key(ec.SECP256R1())
    sender = ephemeral.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = ephemeral.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver))
    ikm = HKDF(hashes.SHA256(), 32, salt=secret, info=b"WebPush: info\x00" + receiver + sender).derive(shared)
    salt = salt or os.urandom(16)
    key = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise PushError("The notification payload is too large")
    ciphertext = AESGCM(key).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack(">I", RECORD_SIZE) + bytes([len(sender)]) + sender + ciphertext


# --- subscriptions and preferences --------------------------------------------------

# The browsers' own push services. A subscription's address is whatever the browser sends, so without this any signed-in person could
# have the NVR post to a server of their choosing every time an alarm fired (#523). NVR_PUSH_EXTRA_HOSTS (comma-separated host names)
# adds the service of a browser that is not on the list.
PUSH_SERVICE_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com")
PUSH_SERVICE_SUFFIXES = (".push.apple.com", ".notify.windows.com")
MAX_DEVICES = 10  # Per person, browsers and phones together: an eleventh takes the place of the one used longest ago
SEND_DEADLINE_SECONDS = 20.0  # One delivery, whatever the service does: every other device's notification waits behind it


def push_service_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    extra = {item.strip().lower() for item in os.environ.get("NVR_PUSH_EXTRA_HOSTS", "").split(",") if item.strip()}
    return bool(host) and (host in PUSH_SERVICE_HOSTS or host in extra or host.endswith(PUSH_SERVICE_SUFFIXES))


def make_room_for_device(db: sqlite3.Connection, user_id: int, endpoint: str) -> None:
    """Before a person's new device is stored: at the limit, the device used longest ago goes. In the caller's transaction."""
    if db.execute("SELECT 1 FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone():
        return  # Known already: it is being updated, not added
    held = db.execute("SELECT id,device_label FROM push_subscriptions WHERE user_id=? ORDER BY COALESCE(last_success_at,created_at) DESC, created_at DESC", (user_id,)).fetchall()
    for stale in held[MAX_DEVICES - 1:]:
        db.execute("DELETE FROM push_subscriptions WHERE id=?", (stale["id"],))
        audit_record(db, user_id, "push_unsubscribed", "push_subscription", stale["id"], device=stale["device_label"], reason="device_limit")


def _post_for_status(client: httpx.Client, endpoint: str, body: bytes, headers: dict[str, str]) -> int:
    """Send one notification and return the service's status. The answer's body is never read, so its size is no concern, and the whole
    attempt has a deadline: a service that answered a byte every few seconds never tripped the per-read time-out and held up everyone's
    notifications, and one that answered with a body that expands enormously grew this process by its expanded size (#523)."""
    outcome: dict[str, Any] = {}

    def post() -> None:
        try:
            with client.stream("POST", endpoint, content=body, headers=headers) as response:
                outcome["status"] = int(response.status_code)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller's thread
            outcome["error"] = exc

    sender = threading.Thread(target=post, name="push-send", daemon=True)
    sender.start()
    sender.join(SEND_DEADLINE_SECONDS)
    if sender.is_alive():
        raise httpx.ReadTimeout("The push service did not answer in time")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["status"]


def validate_endpoint(endpoint: str) -> str:
    """Push services are public HTTPS endpoints; anything else is refused, when stored and when used."""
    from .webhooks import validate_target_url

    if not endpoint.startswith("https://"):
        raise ValueError("Push subscriptions must use an HTTPS push service")
    if not push_service_host(urlsplit(endpoint).hostname or ""):
        raise ValueError("Push subscriptions must use a browser's own push service (Google, Mozilla, Apple or Microsoft)")
    try:
        return validate_target_url(endpoint, allow_private=False)
    except ValueError as exc:
        raise ValueError(str(exc).replace("Webhook", "Push service").replace("webhook", "push service")) from exc


def subscribe(user_id: int, endpoint: str, p256dh: str, auth: str, device_label: str) -> dict[str, Any]:
    endpoint = validate_endpoint(endpoint)
    if len(_unb64(p256dh)) != 65 or len(_unb64(auth)) != 16:
        raise ValueError("The subscription keys are malformed")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            make_room_for_device(db, user_id, endpoint)
            existing = db.execute("SELECT id FROM push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
            subscription_id = existing["id"] if existing else str(uuid.uuid4())
            # A device that changes hands (another person signs in) moves to its new owner.
            db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh,auth,device_label) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,p256dh=excluded.p256dh,auth=excluded.auth,device_label=excluded.device_label",
                       (subscription_id, user_id, endpoint, p256dh, auth, device_label))
            audit_record(db, user_id, "push_subscribed", "push_subscription", subscription_id, device=device_label)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return {"id": subscription_id, "device_label": device_label}


def unsubscribe(user_id: int, endpoint: str) -> bool:
    try:
        stored = validate_endpoint(endpoint)
    except ValueError:
        stored = endpoint
    with connect() as db:
        return db.execute("DELETE FROM push_subscriptions WHERE user_id=? AND endpoint IN (?,?)", (user_id, endpoint, stored)).rowcount > 0


def preferences(db: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    row = db.execute("SELECT * FROM push_preferences WHERE user_id=?", (user_id,)).fetchone()
    devices = db.execute("SELECT id,device_label,created_at,last_success_at,last_failure_at FROM push_subscriptions WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()
    quiet = {"start": row["quiet_start"], "end": row["quiet_end"], "timezone": row["quiet_timezone"]} if row and row["quiet_start"] else None
    return {
        "rule_ids": json.loads(row["rule_ids_json"]) if row and row["rule_ids_json"] is not None else None,
        "group_ids": json.loads(row["group_ids_json"]) if row and row["group_ids_json"] is not None else None,
        "quiet_hours": quiet, "devices": [dict(device) for device in devices],
    }


def save_preferences(user_id: int, rule_ids: list[str] | None, group_ids: list[str] | None, quiet: dict[str, str] | None) -> dict[str, Any]:
    with connect() as db:
        db.execute(
            "INSERT INTO push_preferences(user_id,rule_ids_json,group_ids_json,quiet_start,quiet_end,quiet_timezone,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET rule_ids_json=excluded.rule_ids_json,group_ids_json=excluded.group_ids_json,quiet_start=excluded.quiet_start,"
            "quiet_end=excluded.quiet_end,quiet_timezone=excluded.quiet_timezone,updated_at=excluded.updated_at",
            (user_id, None if rule_ids is None else json.dumps(sorted(set(rule_ids))), None if group_ids is None else json.dumps(sorted(set(group_ids))),
             quiet["start"] if quiet else None, quiet["end"] if quiet else None, quiet["timezone"] if quiet else None, _now().isoformat()))
        return preferences(db, user_id)


def in_quiet_hours(quiet_start: str | None, quiet_end: str | None, zone: str | None, at: datetime) -> bool:
    if not quiet_start or not quiet_end or not zone:
        return False
    try:
        local = at.astimezone(ZoneInfo(zone))
    except ZoneInfoNotFoundError:
        return False
    minute = local.hour * 60 + local.minute
    start = int(quiet_start[:2]) * 60 + int(quiet_start[3:5])
    end = int(quiet_end[:2]) * 60 + int(quiet_end[3:5])
    return start <= minute < end if start < end else minute >= start or minute < end


# --- queueing -------------------------------------------------------------------------

def thumbnail_token(alarm_id: str, now: datetime | None = None) -> str:
    expires = int(((now or _now()) + THUMBNAIL_LIFETIME).timestamp())
    message = f"{alarm_id}.{expires}"
    signature = hmac.new(derived_key("push-thumbnail"), message.encode(), hashlib.sha256).digest()
    return f"{message}.{_b64(signature)}"


def verify_thumbnail_token(token: str, now: datetime | None = None) -> str:
    """The alarm a thumbnail token names, or LookupError when it is forged or expired."""
    try:
        alarm_id, expires, signature = token.rsplit(".", 2)
        expected = hmac.new(derived_key("push-thumbnail"), f"{alarm_id}.{expires}".encode(), hashlib.sha256).digest()
        valid = hmac.compare_digest(_unb64(signature), expected) and int(expires) > (now or _now()).timestamp()
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise LookupError("This thumbnail link has expired")
    return alarm_id


def queue_for_alarm(db: sqlite3.Connection, alarm_id: str) -> int:
    """Queue a notification to every device that wants this alarm, inside the caller's transaction.

    A failure here is logged and undone on its own; it never undoes the alarm.
    """
    db.execute("SAVEPOINT push_queue")
    try:
        queued = _queue_for_alarm(db, alarm_id)
    except Exception:  # noqa: BLE001 - notifications must not cost the alarm
        db.execute("ROLLBACK TO push_queue")
        db.execute("RELEASE push_queue")
        logger.exception("Push notifications for alarm %s could not be queued", alarm_id)
        return 0
    db.execute("RELEASE push_queue")
    return queued


def queue_reminder(db: sqlite3.Connection, alarm_id: str, body: str) -> int:
    """Notify the devices that want this alarm again, with ``body`` (a stretch that is still going, #513). The alarm's earlier
    notification to a device is replaced by this one; one still waiting to be sent is left as it is."""
    db.execute("SAVEPOINT push_reminder")
    try:
        queued = _queue_for_alarm(db, alarm_id, reminder=body)
    except Exception:  # noqa: BLE001 - notifications must not cost the note on the alarm
        db.execute("ROLLBACK TO push_reminder")
        db.execute("RELEASE push_reminder")
        logger.exception("The reminder for alarm %s could not be queued", alarm_id)
        return 0
    db.execute("RELEASE push_reminder")
    return queued


def _queue_for_alarm(db: sqlite3.Connection, alarm_id: str, reminder: str | None = None) -> int:
    alarm = db.execute("SELECT a.*,c.name AS camera_name,c.group_id FROM alarms a JOIN cameras c ON c.id=a.camera_id WHERE a.id=?", (alarm_id,)).fetchone()
    if alarm is None:
        return 0
    occurred = datetime.fromisoformat(alarm["occurred_at"]) if reminder is None else _now()  # A reminder is about now: its quiet hours are now's
    groups = {row[0] for row in db.execute("SELECT group_id FROM cameras WHERE id=? AND group_id IS NOT NULL UNION SELECT group_id FROM camera_group_memberships WHERE camera_id=?",
                                           (alarm["camera_id"], alarm["camera_id"]))}
    label = label_text(alarm["label"])
    site = db.execute("SELECT time_zone FROM site_display_settings WHERE singleton_id=1").fetchone()
    try:
        shown = occurred.astimezone(ZoneInfo(site["time_zone"])) if site else occurred
    except ZoneInfoNotFoundError:
        shown = occurred
    payload = json.dumps({
        "alarm_id": alarm_id, "title": f"{alarm['rule_name']}: {alarm['camera_name']}", "body": reminder or f"{label} at {shown.strftime('%H:%M:%S')}",
        "occurred_at": alarm["occurred_at"], "url": f"/?alarm={alarm_id}", "thumbnail_url": f"/api/push/thumbnails/{thumbnail_token(alarm_id)}",
    }, separators=(",", ":"))
    queued = 0
    now = _now().isoformat()
    for subscription in db.execute(
        "SELECT s.id,s.user_id,p.rule_ids_json,p.group_ids_json,p.quiet_start,p.quiet_end,p.quiet_timezone FROM push_subscriptions s "
        "LEFT JOIN push_preferences p ON p.user_id=s.user_id").fetchall():
        if not may_view_camera(db, subscription["user_id"], alarm["camera_id"]):
            continue
        rules = json.loads(subscription["rule_ids_json"]) if subscription["rule_ids_json"] is not None else None
        chosen_groups = json.loads(subscription["group_ids_json"]) if subscription["group_ids_json"] is not None else None
        if rules is not None and alarm["rule_id"] not in rules:
            continue
        if chosen_groups is not None and groups.isdisjoint(chosen_groups):
            continue
        if in_quiet_hours(subscription["quiet_start"], subscription["quiet_end"], subscription["quiet_timezone"], occurred):
            continue
        if reminder is None:
            queued += db.execute("INSERT OR IGNORE INTO push_deliveries(id,subscription_id,alarm_id,payload_json,next_attempt_at) VALUES(?,?,?,?,?)",
                                 (str(uuid.uuid4()), subscription["id"], alarm_id, payload, now)).rowcount
        else:  # One delivery a device and alarm: the one already made is sent again with the reminder's words
            queued += db.execute(
                "INSERT INTO push_deliveries(id,subscription_id,alarm_id,payload_json,next_attempt_at) VALUES(?,?,?,?,?) ON CONFLICT(subscription_id,alarm_id) DO UPDATE SET "
                "payload_json=excluded.payload_json,status='queued',attempt_count=0,next_attempt_at=excluded.next_attempt_at,last_error=NULL WHERE push_deliveries.status IN ('sent','failed')",
                (str(uuid.uuid4()), subscription["id"], alarm_id, payload, now)).rowcount
    return queued


# --- delivery ---------------------------------------------------------------------------

def _claim(db: sqlite3.Connection) -> sqlite3.Row | None:
    if not has_row(db, "SELECT 1 FROM push_deliveries WHERE status='queued' AND next_attempt_at<=? LIMIT 1", (_now().isoformat(),)):
        return None
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT d.id,d.payload_json,d.attempt_count,s.id AS subscription_id,s.user_id,s.kind,s.endpoint,s.p256dh,s.auth,a.camera_id FROM push_deliveries d "
            "JOIN push_subscriptions s ON s.id=d.subscription_id JOIN alarms a ON a.id=d.alarm_id "
            "WHERE d.status='queued' AND d.next_attempt_at<=? ORDER BY d.next_attempt_at LIMIT 1",
            (_now().isoformat(),)).fetchone()
        if row is not None:
            db.execute("UPDATE push_deliveries SET status='sending',attempt_count=attempt_count+1 WHERE id=?", (row["id"],))
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
    return row


def _finish(delivery: sqlite3.Row, status_code: int | None, error: str | None) -> str:
    now = _now()
    with connect() as db:
        if status_code is not None and 200 <= status_code < 300:
            db.execute("UPDATE push_deliveries SET status='sent',last_error=NULL WHERE id=?", (delivery["id"],))
            db.execute("UPDATE push_subscriptions SET last_success_at=? WHERE id=?", (now.isoformat(), delivery["subscription_id"]))
            return "sent"
        if status_code in (401, 403, 404, 410):
            # The subscription is gone, or belongs to another key: forget the device and its queue.
            db.execute("DELETE FROM push_subscriptions WHERE id=?", (delivery["subscription_id"],))
            return "removed"
        db.execute("UPDATE push_subscriptions SET last_failure_at=? WHERE id=?", (now.isoformat(), delivery["subscription_id"]))
        attempts = int(delivery["attempt_count"]) + 1
        if attempts >= MAX_ATTEMPTS or status_code in (400, 413) or (status_code is None and error and error.startswith("invalid:")):
            db.execute("UPDATE push_deliveries SET status='failed',last_error=? WHERE id=?", (error, delivery["id"]))
            return "failed"
        retry = now + timedelta(seconds=RETRY_SECONDS[min(attempts - 1, len(RETRY_SECONDS) - 1)])
        db.execute("UPDATE push_deliveries SET status='queued',next_attempt_at=?,last_error=? WHERE id=?", (retry.isoformat(), error, delivery["id"]))
        return "retrying"


PRUNE_EVERY_SECONDS = 3600.0
_pruned_at = float("-inf")


def prune_deliveries(*, force: bool = False) -> int:
    """Remove finished deliveries older than a week, at most once an hour, and only when there are any: it used to be a delete at the end of
    every two-second pass, which took the database's write lock and read every finished row even with nothing to remove (#525)."""
    global _pruned_at
    if not force and time.monotonic() - _pruned_at < PRUNE_EVERY_SECONDS:
        return 0
    _pruned_at = time.monotonic()
    old = ((_now() - KEEP_DELIVERIES).isoformat(),)
    with connect() as db:
        if not has_row(db, "SELECT 1 FROM push_deliveries WHERE status IN ('sent','failed') AND created_at<? LIMIT 1", old):
            return 0
        return db.execute("DELETE FROM push_deliveries WHERE status IN ('sent','failed') AND created_at<?", old).rowcount


def deliver_due(client: httpx.Client | None = None, limit: int = 50) -> list[str]:
    """Send queued notifications that are due; return each outcome."""
    outcomes: list[str] = []
    owned = client is None
    client = client or httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
    native_client: httpx.Client | None = None if owned else client  # Apple's service speaks only HTTP/2
    try:
        with connect() as db:
            private, public = _keys(db)
        for _ in range(limit):
            with connect() as db:
                delivery = _claim(db)
            if delivery is None:
                break
            with connect() as db:
                allowed = may_view_camera(db, delivery["user_id"], delivery["camera_id"])
            if not allowed:
                # The device changed hands, or its owner lost access, since this was queued.
                outcomes.append(_finish(delivery, None, "invalid: the recipient may no longer see this camera"))
                continue
            try:
                if delivery["kind"] != "web":
                    from . import native_push
                    native_client = native_client or httpx.Client(timeout=10, follow_redirects=False, trust_env=False, http2=True)
                    status_code, error = native_push.send(native_client, delivery["kind"], delivery["endpoint"].split(":", 1)[1], json.loads(delivery["payload_json"]))
                    outcomes.append(_finish(delivery, status_code, error))
                    continue
                validate_endpoint(delivery["endpoint"])
                body = encrypt_payload(delivery["payload_json"].encode("utf-8"), delivery["p256dh"], delivery["auth"])
                status_code = _post_for_status(client, delivery["endpoint"], body, {
                    "Authorization": vapid_header(delivery["endpoint"], private, public), "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream", "TTL": str(TTL_SECONDS), "Urgency": "high",
                })
                outcomes.append(_finish(delivery, status_code, None if 200 <= status_code < 300 else f"HTTP {status_code}"))
            except (PushError, ValueError) as exc:
                outcomes.append(_finish(delivery, None, f"invalid: {str(exc)[:180]}"))
            except httpx.HTTPError as exc:
                outcomes.append(_finish(delivery, None, str(exc)[:200] or exc.__class__.__name__))
        prune_deliveries()
    finally:
        if owned:
            client.close()
            if native_client is not None:
                native_client.close()
    return outcomes


_THUMBNAILS: dict[tuple[str, str], bytes] = {}


def thumbnail(alarm_id: str) -> bytes | None:
    """The alarm's picture: a frame from its recording once indexed, else its object's keyframe."""
    import shutil
    import subprocess

    from .retention import acquire_playback, release_playback
    from .storage import recording_file_path

    with connect() as db:
        alarm = db.execute("SELECT camera_id,occurred_at,track_id FROM alarms WHERE id=?", (alarm_id,)).fetchone()
        if alarm is None:
            return None
        segment = db.execute(
            "SELECT id,relative_path,starts_at FROM recording_segments WHERE camera_id=? AND starts_at<=? AND ends_at>? AND unavailable_at IS NULL ORDER BY starts_at DESC LIMIT 1",
            (alarm["camera_id"], alarm["occurred_at"], alarm["occurred_at"])).fetchone()
        keyframe = db.execute("SELECT jpeg,relative_path,captured_at FROM track_keyframes WHERE track_id=?", (alarm["track_id"],)).fetchone() if alarm["track_id"] else None
        from .privacy import drawbox_filter, mask_jpeg_sync, server_zones

        segment_zones = server_zones(db, alarm["camera_id"], datetime.fromisoformat(segment["starts_at"])) if segment is not None else []
        keyframe_zones = server_zones(db, alarm["camera_id"], datetime.fromisoformat(keyframe["captured_at"])) if keyframe is not None else []
    if segment is not None and shutil.which("ffmpeg"):
        cache_key = (alarm_id, str(segment["id"]), json.dumps(segment_zones))
        if cache_key in _THUMBNAILS:
            return _THUMBNAILS[cache_key]
        try:
            path = recording_file_path(segment["relative_path"])
        except (OSError, ValueError):
            path = None
        if path is not None and path.is_file():
            offset = max(0.0, (datetime.fromisoformat(alarm["occurred_at"]) - datetime.fromisoformat(segment["starts_at"])).total_seconds())
            acquire_playback(path)
            try:
                result = subprocess.run(
                    [shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", "-ss", f"{offset:.3f}", "-i", str(path),
                     "-frames:v", "1", "-vf", (f"{drawbox_filter(segment_zones)}," if segment_zones else "") + "scale=640:-2:force_original_aspect_ratio=decrease",
                     "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                    capture_output=True, timeout=15, check=False)
            except subprocess.TimeoutExpired:
                result = None
            finally:
                release_playback(path)
            if result is not None and result.returncode == 0 and result.stdout.startswith(b"\xff\xd8"):
                if len(_THUMBNAILS) >= 64:
                    _THUMBNAILS.pop(next(iter(_THUMBNAILS)))
                _THUMBNAILS[cache_key] = result.stdout
                return result.stdout
    # Privacy masks apply to the keyframe too; if one cannot be drawn there is no picture at all.
    from . import track_pictures

    picture = track_pictures.read(keyframe)  # A file on the recordings volume (#512), or the row's own picture for an older one
    return mask_jpeg_sync(picture, keyframe_zones) if picture else None


def recover_inflight() -> None:
    """Deliveries that were being sent when the worker stopped are sent again, unless they have had their attempts: a delivery that kills
    the worker was put back and sent again after every restart, without end (#523)."""
    with connect() as db:
        db.execute("UPDATE push_deliveries SET status='failed',last_error='The worker stopped while sending this, more than once' WHERE status='sending' AND attempt_count>=?", (MAX_ATTEMPTS,))
        db.execute("UPDATE push_deliveries SET status='queued' WHERE status='sending'")


class PushManager:
    """Delivers queued push notifications in the background worker."""

    def __init__(self, poll_seconds: float = 2.0) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await asyncio.to_thread(recover_inflight)
        self.task = asyncio.create_task(self._run(), name="push-delivery")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await asyncio.to_thread(deliver_due)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep delivering; failures are recorded per delivery
                logger.exception("Push delivery cycle failed")
            await asyncio.sleep(max(0.0, self.poll_seconds - (time.monotonic() - started)))
