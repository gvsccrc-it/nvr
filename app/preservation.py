from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from .storage import remove_segment_files, segment_available
from .store import connect
from .audit import record as audit_record


def active_hold(db, segment_id: int, now: datetime | None = None):
    now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return db.execute(
        "SELECT * FROM recording_holds WHERE segment_id=? AND released_at IS NULL AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC LIMIT 1",
        (segment_id,now),
    ).fetchone()


# Limits on the holds an operator places (#491, the owner's decision). Held footage is never removed to make room, an operator's hold never
# expired, and nothing limited how many they placed: holds alone could fill a disk. A hold placed by an operator now expires after
# OPERATOR_HOLD_DAYS (an administrator can extend it or make it indefinite), and all of one operator's holds together may cover at most
# OPERATOR_SHARE of a recordings volume. Holds that belong to a case follow the case, not these limits.
OPERATOR_HOLD_DAYS = 30
OPERATOR_SHARE = 0.10


class HoldLimit(Exception):
    """An operator's holds would pass their share of the volume."""


def _volume_capacity(relative_path: str) -> tuple[str, int | None]:
    """The volume a segment is on and that volume's size (None when it cannot be read: the limit is then not applied)."""
    import shutil

    from .headroom import volume_of
    from .storage import configured_volumes, volume_path

    volumes = configured_volumes()
    volume_id = volume_of(relative_path, volumes)
    try:
        return volume_id, int(shutil.disk_usage(volume_path(volume_id)).total)
    except (OSError, ValueError):
        return volume_id, None


def operator_held_bytes(db, actor_id: int, volume_id: str, now: datetime) -> int:
    """How much footage on a volume one person's own holds (not a case's) cover now."""
    from .storage import configured_volumes
    from .volume_marker import _prefix_filter

    where, values = _prefix_filter(volume_id, configured_volumes(), available_only=False)
    return int(db.execute(
        f"SELECT COALESCE(SUM(size_bytes),0) FROM recording_segments WHERE {where} AND id IN (SELECT segment_id FROM recording_holds WHERE created_by=? AND case_id IS NULL "
        "AND released_at IS NULL AND (expires_at IS NULL OR expires_at>?))", (*values, actor_id, now.isoformat())).fetchone()[0])


def preserve_segment(camera_id: str, segment_id: int, actor_id: int, expires_in_days: int | None = None, *, operator: bool = False) -> dict[str, object]:
    """Place a hold. ``operator``: the person is not an administrator, so the hold expires after OPERATOR_HOLD_DAYS and counts towards
    their share of the volume."""
    hold_id=str(uuid.uuid4())
    now=datetime.now(timezone.utc)
    if operator:
        expires_in_days=OPERATOR_HOLD_DAYS
    expires=(now+timedelta(days=expires_in_days)).isoformat() if expires_in_days else None
    with connect() as db:
        segment=db.execute("SELECT id,relative_path,size_bytes FROM recording_segments WHERE id=? AND camera_id=?",(segment_id,camera_id)).fetchone()
        capacity=_volume_capacity(segment["relative_path"]) if operator and segment is not None else None  # A disk read: before the write lock is taken
        db.execute("BEGIN IMMEDIATE")
        segment=db.execute("SELECT id,relative_path,size_bytes FROM recording_segments WHERE id=? AND camera_id=?",(segment_id,camera_id)).fetchone()
        if segment is None: raise LookupError("Recorded interval not found")
        existing=active_hold(db,segment_id)
        if existing: raise FileExistsError("Recorded interval is already preserved (by a hold or a case)")
        if not segment_available(segment["relative_path"]): raise FileNotFoundError("Recorded interval is unavailable")
        if capacity is not None and capacity[1]:
            held=operator_held_bytes(db,actor_id,capacity[0],now)
            if held+int(segment["size_bytes"])>capacity[1]*OPERATOR_SHARE:
                raise HoldLimit(f"Your holds already cover {held/(1<<30):.1f} GB of this recordings volume, and together they may cover at most {int(OPERATOR_SHARE*100)}% of it. "
                                "Ask an administrator to place this hold, or to release holds that are no longer needed.")
        db.execute("INSERT INTO recording_holds(id,segment_id,camera_id,created_by,expires_at) VALUES(?,?,?,?,?)",(hold_id,segment_id,camera_id,actor_id,expires))
        audit_record(db, actor_id, "preserve", "recording_segment", str(segment_id), camera_id=camera_id, expires_at=expires)
    return {"id":hold_id,"segment_id":segment_id,"camera_id":camera_id,"created_by":actor_id,"expires_at":expires,"released_at":None}


def release_segment_hold(camera_id: str, segment_id: int, actor_id: int) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        now=datetime.now(timezone.utc).isoformat()
        # Only a hold someone placed by hand: footage an open case holds stays held until the case lets it go.
        hold=db.execute("SELECT * FROM recording_holds WHERE segment_id=? AND case_id IS NULL AND released_at IS NULL AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC LIMIT 1",(segment_id,now)).fetchone()
        if hold is None and active_hold(db,segment_id) is not None: raise PermissionError("This footage is held for a case; remove the clip or close the case to release it")
        if hold is None or hold["camera_id"]!=camera_id: raise LookupError("No active preservation hold found")
        db.execute("UPDATE recording_holds SET released_by=?,released_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",(actor_id,hold["id"]))
        audit_record(db, actor_id, "release_hold", "recording_segment", str(segment_id), camera_id=camera_id, hold_id=hold["id"])


def extend_segment_hold(camera_id: str, segment_id: int, actor_id: int, expires_in_days: int | None) -> dict[str, object]:
    """An administrator gives a hold a new expiry, counted from now, or none at all (``None``: it is kept until released)."""
    now=datetime.now(timezone.utc)
    expires=(now+timedelta(days=expires_in_days)).isoformat() if expires_in_days else None
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        hold=db.execute("SELECT * FROM recording_holds WHERE segment_id=? AND camera_id=? AND case_id IS NULL AND released_at IS NULL AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC LIMIT 1",
                        (segment_id,camera_id,now.isoformat())).fetchone()
        if hold is None and active_hold(db,segment_id) is not None: raise PermissionError("This footage is held for a case; it is kept for as long as the case holds it")
        if hold is None: raise LookupError("No active preservation hold found")
        db.execute("UPDATE recording_holds SET expires_at=? WHERE id=?",(expires,hold["id"]))
        audit_record(db, actor_id, "extend_hold", "recording_segment", str(segment_id), camera_id=camera_id, hold_id=hold["id"], expires_at=expires, was=hold["expires_at"])
    return {"id":hold["id"],"segment_id":segment_id,"camera_id":camera_id,"created_by":hold["created_by"],"expires_at":expires,"released_at":None}


def delete_segment(camera_id: str, segment_id: int, actor_id: int) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        segment=db.execute("SELECT id,relative_path,tier_id,tier_key,tier_sha256 FROM recording_segments WHERE id=? AND camera_id=?",(segment_id,camera_id)).fetchone()
        if segment is None: raise LookupError("Recorded interval not found")
        if active_hold(db,segment_id): raise PermissionError("Preserved footage cannot be deleted")
        # The camera-wide delete and retention both leave footage a queued or running export will read; this one did not (#361).
        if db.execute("SELECT 1 FROM export_sources s JOIN exports e ON e.id=s.export_id WHERE s.segment_id=? AND e.status IN ('queued','running') LIMIT 1",(segment_id,)).fetchone():
            raise PermissionError("This footage is in a queued or running export; delete it once the export has finished")
        if not remove_segment_files([segment]): raise BlockingIOError("Recorded interval is currently being played")
        db.execute("DELETE FROM recording_segments WHERE id=?",(segment_id,))
        audit_record(db, actor_id, "delete", "recording_segment", str(segment_id), camera_id=camera_id)
    from .tiering import discard_objects
    discard_objects([segment])  # A copy in an object store goes once the row is gone


def get_segment_hold(camera_id: str, segment_id: int) -> dict[str, object]:
    with connect() as db:
        if db.execute("SELECT 1 FROM recording_segments WHERE id=? AND camera_id=?",(segment_id,camera_id)).fetchone() is None:
            raise LookupError("Recorded interval not found")
        hold=active_hold(db,segment_id)
    if hold is None: return {"camera_id":camera_id,"segment_id":segment_id,"held":False,"hold":None}
    return {"camera_id":camera_id,"segment_id":segment_id,"held":True,"hold":{key:hold[key] for key in ("id","created_by","created_at","expires_at")}}
