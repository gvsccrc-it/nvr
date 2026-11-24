"""Confirming that a camera still holds the privacy masks it accepted (#468), by the owner's decision on the ticket.

A camera that applies its own masks is asked for them (ONVIF ``GetMasks``) on every health pass (``CHECK_EVERY``), when its stream reconnects,
and when its source or login is saved (``request_check``; the next pass, within a minute, asks). The NVR fails closed:

- **Missing or different** (a factory reset, a firmware update, someone editing the camera, a different camera at the same address): the NVR
  draws the masks itself from that moment and raises an incident.
- **Could not be asked** for ``UNREACHABLE_LIMIT``: the same. Until then it is asked again sooner (``RECHECK_UNREACHABLE``), so the limit holds.
- **Confirmed** again: the NVR trusts the camera again and the incident recovers.

Footage recorded while the masks were unconfirmed is masked on the way out (``privacy.server_zones`` reads the gaps). A gap starts at the
last confirmation, because nobody knows when between two checks the masks went. Open live streams restart when trust changes
(``trust_version``, which the once-a-second stream check reads), so a viewer is never left on an unmasked stream.

An idle pass only reads (#280): nothing is written unless a camera is due.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import camera_destination
from .clock import now as _now
from .credentials import decrypt
from .incidents import sync_incident
from .store import connect, has_row

logger = logging.getLogger("nvr.privacy_check")

CHECK_EVERY = timedelta(minutes=10)
RECHECK_UNREACHABLE = timedelta(minutes=4)
UNREACHABLE_LIMIT = timedelta(minutes=15)
CHECK_TIMEOUT_SECONDS = 30
CONCURRENCY = 4

CONFIRMED, MISSING, UNREACHABLE = "confirmed", "missing", "unreachable"

_DUE = ("FROM privacy_masks WHERE camera_side='applied' AND zones_json!='[]' AND written_to IS NOT NULL AND "
        "(check_requested=1 OR checked_at IS NULL OR checked_at<? OR (unreachable_since IS NOT NULL AND checked_at<?))")


def request_check(db: sqlite3.Connection, camera_id: str) -> None:
    """Ask about this camera's masks at the next pass: its stream reconnected, or its source or login changed. Writes only when it applies masks."""
    db.execute("UPDATE privacy_masks SET check_requested=1 WHERE camera_id=? AND camera_side='applied' AND check_requested=0", (camera_id,))


def due(db: sqlite3.Connection, now: datetime) -> list[str]:
    since_check, since_retry = (now - CHECK_EVERY).isoformat(), (now - RECHECK_UNREACHABLE).isoformat()
    return [row[0] for row in db.execute(f"SELECT camera_id {_DUE} ORDER BY checked_at IS NOT NULL, checked_at LIMIT 200", (since_check, since_retry))]


def outcome(found: dict[str, tuple[bool, tuple[tuple[float, float], ...]]], tokens: list[str], zones: list[dict[str, float]]) -> str:
    """Whether what the camera holds is what the NVR wrote: every one of its masks there, switched on, and shaped as the areas drawn."""
    from .onvif_masks import expected_polygons

    if not tokens:
        return MISSING
    expected = expected_polygons(zones)
    held = [found.get(token) for token in tokens]
    if any(mask is None or not mask[0] or mask[1] not in expected for mask in held):
        return MISSING
    return CONFIRMED if {mask[1] for mask in held if mask} == expected else MISSING


async def ask(camera_id: str) -> str:
    """Ask the camera for its masks and compare. Never raises: a camera that could not be asked is ``UNREACHABLE``."""
    from .onvif_masks import MasksUnsupported, read_masks

    with connect() as db:
        row = db.execute("SELECT m.zones_json,m.camera_mask_tokens_json,m.written_to,c.device_service_url,c.motion_credentials_ciphertext,c.public_address "
                         "FROM privacy_masks m JOIN cameras c ON c.id=m.camera_id WHERE m.camera_id=?", (camera_id,)).fetchone()
    if row is None or not row["device_service_url"] or row["device_service_url"] != row["written_to"]:
        return MISSING  # Not the device the masks were written to: never trusted (``privacy.server_zones`` already masks)
    credentials = json.loads(decrypt(row["motion_credentials_ciphertext"])) if row["motion_credentials_ciphertext"] else {}
    try:
        with camera_destination.public_permitted(camera_destination.camera_allows_public(row)):  # The camera address rule (#474)
            found = await asyncio.wait_for(read_masks(row["device_service_url"], credentials.get("username", ""), credentials.get("password", "")),
                                           CHECK_TIMEOUT_SECONDS)
    except MasksUnsupported:
        return MISSING
    except Exception:  # noqa: BLE001 - whatever the reason, it could not be asked; never logged with the camera's address or login
        return UNREACHABLE
    return outcome(found, json.loads(row["camera_mask_tokens_json"]), json.loads(row["zones_json"]))


def record(camera_id: str, result: str, now: datetime | None = None) -> None:
    """Store what the check found, open or close the gap, and raise or recover the incident, in one transaction."""
    now = now or _now()
    stamp = now.isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM privacy_masks WHERE camera_id=? AND camera_side='applied'", (camera_id,)).fetchone()
            if row is None:
                db.execute("COMMIT")
                return
            gap = db.execute("SELECT id FROM privacy_mask_gaps WHERE camera_id=? AND ended_at IS NULL", (camera_id,)).fetchone()
            unreachable_since = row["unreachable_since"]
            if result == CONFIRMED:
                unreachable_since = None
            elif result == UNREACHABLE and unreachable_since is None:
                unreachable_since = stamp
            failing = result == MISSING or (result == UNREACHABLE and now - datetime.fromisoformat(unreachable_since) >= UNREACHABLE_LIMIT)
            changed = False
            if failing and gap is None:
                started = row["confirmed_at"] or row["camera_masked_since"] or stamp
                db.execute("INSERT INTO privacy_mask_gaps(camera_id,started_at,reason) VALUES(?,?,?)", (camera_id, started, result))
                changed = True
            elif result == CONFIRMED and gap is not None:
                db.execute("UPDATE privacy_mask_gaps SET ended_at=? WHERE id=?", (stamp, gap["id"]))
                changed = True
            db.execute("UPDATE privacy_masks SET checked_at=?,check_requested=0,unreachable_since=?,"
                       "confirmed_at=CASE WHEN ? THEN ? ELSE confirmed_at END,trust_version=trust_version+? WHERE camera_id=?",
                       (stamp, unreachable_since, result == CONFIRMED, stamp, int(changed), camera_id))
            if changed or (failing and gap is not None) or result == CONFIRMED:
                _incident(db, camera_id, active=failing or (result == UNREACHABLE and gap is not None), result=result)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def _incident(db: sqlite3.Connection, camera_id: str, *, active: bool, result: str) -> None:
    name = db.execute("SELECT name FROM cameras WHERE id=?", (camera_id,)).fetchone()
    reason = ("the camera could not be asked for its masks for 15 minutes" if result == UNREACHABLE
              else "the camera no longer holds the privacy masks the NVR wrote to it")
    sync_incident(db, incident_key=f"privacy_masks:{camera_id}", camera_id=camera_id, fault_type="privacy_masks", active=active,
                  summary=f"Privacy masks on {name[0] if name else 'a camera'} are not confirmed: {reason}. The NVR is drawing the masks itself, "
                          "live and on footage recorded since they were last confirmed.",
                  details={"result": result})


async def check(camera_id: str) -> str:
    result = await ask(camera_id)
    await asyncio.to_thread(record, camera_id, result)
    return result


async def check_due(now: datetime | None = None) -> int:
    """One pass: ask every camera that is due. Reads first and writes nothing when none is (#280). Returns how many were asked."""
    now = now or _now()

    def find() -> list[str]:
        with connect() as db:
            since = ((now - CHECK_EVERY).isoformat(), (now - RECHECK_UNREACHABLE).isoformat())
            return due(db, now) if has_row(db, f"SELECT 1 {_DUE} LIMIT 1", since) else []

    cameras = await asyncio.to_thread(find)
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(camera_id: str) -> None:
        async with gate:
            try:
                await check(camera_id)
            except Exception:  # noqa: BLE001 - one camera must not stop the others
                logger.exception("The privacy masks of camera %s could not be checked", camera_id)

    await asyncio.gather(*(one(camera_id) for camera_id in cameras))
    return len(cameras)


def state(db: sqlite3.Connection, camera_id: str) -> dict[str, Any]:
    """For the camera's privacy page: when the masks were last confirmed on the camera, and since when they have not been."""
    row = db.execute("SELECT confirmed_at,checked_at,unreachable_since,always_server FROM privacy_masks WHERE camera_id=?", (camera_id,)).fetchone()
    gap = db.execute("SELECT started_at,reason FROM privacy_mask_gaps WHERE camera_id=? AND ended_at IS NULL", (camera_id,)).fetchone()
    return {"confirmed_at": row["confirmed_at"] if row else None, "checked_at": row["checked_at"] if row else None,
            "unreachable_since": row["unreachable_since"] if row else None, "always_server": bool(row["always_server"]) if row else False,
            "unconfirmed_since": gap["started_at"] if gap else None, "unconfirmed_reason": gap["reason"] if gap else None}
