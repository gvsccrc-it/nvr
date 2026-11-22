"""Storage headroom (#219): storage space comes before retention policy.

A recorder fills its disk by design, so each storage volume keeps a **headroom** of free space. When free space is below it, the oldest footage on the volume is deleted, whatever its
retention policy says, until the headroom is free again. Footage that is held, reserved by an export, covered by an alarm or being played is never deleted; if that alone fills the
volume the shortfall is reported beside the setting instead.
"""
from __future__ import annotations

import heapq
import itertools
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .store import connect, has_row

GIB = 1 << 30
DEFAULT_SHARE = 0.05
DEFAULT_FLOOR_BYTES = 5 * GIB
MAX_SEGMENTS_PER_PASS = 5000
MAX_SHARE = 0.9  # A headroom is never more than this much of the volume, so a mistyped one cannot delete everything over and over
KEEP_LOG_DAYS = 30
RECENT_SECONDS = 600  # Footage removed within this long means the volume is "keeping its headroom now"


def default_headroom(capacity_bytes: int | None) -> int:
    """5% of the volume, and at least 5 GiB, so a site that never sets it still keeps room for exports and for the recorder."""
    return max(DEFAULT_FLOOR_BYTES, int((capacity_bytes or 0) * DEFAULT_SHARE))


def chosen_headroom(db: sqlite3.Connection, volume_id: str) -> int | None:
    row = db.execute("SELECT headroom_bytes FROM storage_headroom WHERE volume_id=?", (volume_id,)).fetchone()
    return None if row is None or row["headroom_bytes"] is None else int(row["headroom_bytes"])


def effective_headroom(db: sqlite3.Connection, volume_id: str, capacity_bytes: int | None) -> int:
    chosen = chosen_headroom(db, volume_id)
    wanted = default_headroom(capacity_bytes) if chosen is None else chosen
    return min(wanted, int(capacity_bytes * MAX_SHARE)) if capacity_bytes else wanted


def set_headroom(volume_id: str, headroom_bytes: int | None) -> None:
    """Choose a volume's headroom (None: the default again)."""
    with connect() as db:
        db.execute(
            "INSERT INTO storage_headroom(volume_id,headroom_bytes) VALUES(?,?) "
            "ON CONFLICT(volume_id) DO UPDATE SET headroom_bytes=excluded.headroom_bytes,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (volume_id, headroom_bytes))


def volume_of(relative_path: str, volumes: dict[str, str]) -> str:
    """The storage volume a segment's path is under: the longest configured volume path that prefixes it, else the default volume."""
    best, name = -1, "default"
    for volume_id, relative in volumes.items():
        prefix = relative.strip("/")
        if (not prefix or relative_path == prefix or relative_path.startswith(prefix + "/")) and len(prefix) > best:
            best, name = len(prefix), volume_id
    return name


PAGE = 100  # Segments read at a time for one camera


def _oldest_of_camera(camera_id: str, where: str, values: list[str], now: datetime) -> Iterator[sqlite3.Row]:
    """One camera's hot footage on a volume that nothing in the database protects, oldest first, a page at a time through the
    (camera, start) index. Held and reserved footage is left out in the query, so a long run of it at the front cannot use up the pass."""
    after = ""
    while True:
        with connect() as db:
            rows = db.execute(
                f"SELECT id,relative_path,starts_at,ends_at FROM recording_segments s WHERE camera_id=? AND starts_at>? AND {where} "
                "AND NOT EXISTS (SELECT 1 FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>?)) "
                "AND NOT EXISTS (SELECT 1 FROM export_sources x JOIN exports e ON e.id=x.export_id WHERE x.segment_id=s.id AND e.status IN ('queued','running')) "
                f"ORDER BY starts_at LIMIT {PAGE}", (camera_id, after, *values, now.isoformat())).fetchall()
        yield from rows
        if len(rows) < PAGE:
            return
        after = rows[-1]["starts_at"]


def oldest_removable(volume_id: str, volumes: dict[str, str], now: datetime) -> Iterator[sqlite3.Row]:
    """A volume's removable footage, oldest first across its cameras, read as it is asked for.

    This used to select every hot segment on the server, sort them all, turn each into an object and filter by volume in Python, to keep
    the first 5,000: 1.8 s and 334 MB for a million segments, every minute for every volume that was below its headroom, which is the
    normal state of a recorder (#480). Each camera's oldest are now read through the index, a page at a time, and merged by end time;
    a pass that needs 200 segments reads a few hundred rows."""
    from .volume_marker import _prefix_filter

    where, values = _prefix_filter(volume_id, volumes, available_only=False)
    with connect() as db:
        cameras = [row[0] for row in db.execute("SELECT id FROM cameras")]
    return heapq.merge(*(_oldest_of_camera(camera_id, where, values, now) for camera_id in cameras), key=lambda row: row["ends_at"])


def _record_check(volume_id: str, now: datetime, short_bytes: int) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO storage_headroom(volume_id,last_checked_at,short_bytes) VALUES(?,?,?) "
            "ON CONFLICT(volume_id) DO UPDATE SET last_checked_at=excluded.last_checked_at,short_bytes=excluded.short_bytes",
            (volume_id, now.isoformat(), short_bytes))


# --- saying so when free space cannot be kept (#491) -------------------------------------------------------------------------------
#
# Headroom never deletes footage that is held, kept for an alarm, reserved by an export or being played. When that alone keeps a volume
# below its headroom, the shortfall was a number beside a setting and nothing else: the first incident was "pressure" at 1 GiB free,
# seconds before the disk was full and every camera stopped. An incident is now raised on the first pass that ends short, saying how
# much is short and what is holding the space, and recovers on the first pass that is not.

SHORT_KEY = "headroom:"
REFRESH_SECONDS = 600.0  # While it stays short, the incident's figures are brought up to date this often
_said: dict[str, tuple[bool, float]] = {}  # Volume -> (short when last written, when): written when it changes, not every minute


def _size(count: int) -> str:
    return f"{count / (1 << 30):.1f} GB" if count >= 1 << 29 else f"{count / (1 << 20):.0f} MB"


def protected_bytes(db: sqlite3.Connection, volume_id: str, volumes: dict[str, str], now: datetime) -> dict[str, object]:
    """What on a volume may not be removed to make room, in bytes: footage held (and by whom), held for cases, kept for alarms, and in
    exports. A segment protected twice is counted under each. Read from the protecting rows' side, so it costs what is protected."""
    from .alarms import resolved_kept_since
    from .volume_marker import _prefix_filter

    where, values = _prefix_filter(volume_id, volumes, available_only=False)
    active = "h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>?)"
    holders = [{"who": row["who"] or "someone no longer here", "bytes": int(row["bytes"])} for row in db.execute(
        f"SELECT u.username AS who,SUM(s.size_bytes) AS bytes FROM recording_holds h JOIN recording_segments s ON s.id=h.segment_id LEFT JOIN users u ON u.id=h.created_by "
        f"WHERE h.case_id IS NULL AND {active} AND {where} GROUP BY h.created_by ORDER BY bytes DESC", (now.isoformat(), *values))]
    cases = db.execute(f"SELECT COALESCE(SUM(s.size_bytes),0) FROM recording_holds h JOIN recording_segments s ON s.id=h.segment_id WHERE h.case_id IS NOT NULL AND {active} AND {where}",
                       (now.isoformat(), *values)).fetchone()[0]
    exports = db.execute(f"SELECT COALESCE(SUM(s.size_bytes),0) FROM export_sources x JOIN exports e ON e.id=x.export_id JOIN recording_segments s ON s.id=x.segment_id "
                         f"WHERE e.status IN ('queued','running') AND {where}", values).fetchone()[0]
    alarms = db.execute(
        f"SELECT COALESCE(SUM(size_bytes),0) FROM (SELECT DISTINCT s.id,s.size_bytes FROM alarms a JOIN recording_segments s ON s.camera_id=a.camera_id AND s.starts_at<a.clip_ends_at "
        f"AND s.ends_at>a.clip_starts_at WHERE (a.state!='resolved' OR a.resolved_at>=?) AND {where})", (resolved_kept_since(db, now), *values)).fetchone()[0]
    return {"held_by": holders, "held_bytes": sum(item["bytes"] for item in holders), "case_bytes": int(cases), "alarm_bytes": int(alarms), "export_bytes": int(exports)}


def _say_shortfall(volume_id: str, volumes: dict[str, str], now: datetime, short: int, wanted: int) -> None:
    """Raise, refresh or recover the volume's "free space cannot be kept" incident. Never stops the pass."""
    import time

    from .incidents import sync_incident

    is_short = short > 0
    last = _said.get(volume_id)
    if last is not None and last[0] == is_short and (not is_short or time.monotonic() - last[1] < REFRESH_SECONDS):
        return
    try:
        with connect() as db:
            if not is_short and last is None and not has_row(db, "SELECT 1 FROM incidents WHERE incident_key=? AND fault_active=1", (SHORT_KEY + volume_id,)):
                _said[volume_id] = (False, time.monotonic())
                return  # Nothing was raised and nothing is wrong: nothing to write
            what = protected_bytes(db, volume_id, volumes, now) if is_short else {}
            if is_short:
                people = ", ".join(f"{item['who']} {_size(item['bytes'])}" for item in what["held_by"][:5])  # type: ignore[index]
                parts = [text for text in (
                    f"{_size(what['held_bytes'])} is held ({people})" if what["held_bytes"] else "",  # type: ignore[arg-type]
                    f"{_size(what['case_bytes'])} is held for cases" if what["case_bytes"] else "",  # type: ignore[arg-type]
                    f"{_size(what['alarm_bytes'])} is kept for alarms" if what["alarm_bytes"] else "",  # type: ignore[arg-type]
                    f"{_size(what['export_bytes'])} is in exports" if what["export_bytes"] else "") if text]  # type: ignore[arg-type]
                summary = (f"Recording storage {volume_id} cannot keep its free space: it is {_size(short)} short of the {_size(wanted)} it keeps free, and nothing more may be "
                           f"removed to make room. {'; '.join(parts) or 'What is left is being played or exported'}. Release holds, close cases or resolve alarms that are no "
                           "longer needed: when the disk is full, every camera on it stops recording.")
            else:
                summary = f"Recording storage {volume_id} has its free space again"
            db.execute("BEGIN IMMEDIATE")
            try:
                sync_incident(db, incident_key=SHORT_KEY + volume_id, camera_id=None, fault_type="storage_headroom", summary=summary, active=is_short,
                              details={"volume_id": volume_id, "short_bytes": short, "headroom_bytes": wanted, **what})
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        _said[volume_id] = (is_short, time.monotonic())
    except sqlite3.Error:
        pass  # Said on a later pass; removing footage matters more than saying so


def keep_headroom(now: datetime | None = None) -> dict[str, dict[str, int]]:
    """One pass over every available volume: delete the oldest removable footage until each has its headroom free (or nothing removable is left)."""
    from .alarms import resolved_kept_since
    from .retention import remove_segment
    from . import volume_marker
    from .storage import configured_volumes, volume_path
    from .tiering import discard_objects

    now = now or datetime.now(timezone.utc)
    volumes = configured_volumes()
    results: dict[str, dict[str, int]] = {}
    for volume_id in volumes:
        try:
            path = volume_path(volume_id)
            if volume_id != "default" and not os.path.ismount(path):
                continue  # Not mounted: what is measured here would be the disk under the empty folder, and its footage is not there to remove (#338)
            if not volume_marker.verified(volume_id, path):
                # The same for any volume, the default one included, whose folder is there without its disk (#479): nothing is removed.
                results[volume_id] = {"removed_segments": 0, "removed_bytes": 0, "short_bytes": 0, "waiting_for_storage": 1}
                continue
            usage = shutil.disk_usage(path)
        except (OSError, ValueError):
            continue
        with connect() as db:
            wanted = effective_headroom(db, volume_id, usage.total)
        free = usage.free
        removed_segments = removed_bytes = 0
        newest = None
        if free < wanted:
            with connect() as db:
                kept_since = resolved_kept_since(db, now)
            candidates = oldest_removable(volume_id, volumes, now)
            for candidate in itertools.islice(candidates, MAX_SEGMENTS_PER_PASS):
                if free >= wanted:
                    break
                try:
                    with connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        outcome, current = remove_segment(db, candidate["id"], now, kept_since, policy_applies=False)
                except (OSError, sqlite3.Error):
                    continue
                if outcome != "removed":
                    continue
                discard_objects([current])
                removed_segments += 1
                removed_bytes += int(current["size_bytes"])
                newest = max(newest or current["ends_at"], current["ends_at"])
                free += int(current["size_bytes"])
                if removed_segments % 20 == 0:  # The files' sizes are close to what was freed; re-read the disk now and then
                    free = shutil.disk_usage(path).free
            free = shutil.disk_usage(path).free
        if removed_segments:
            with connect() as db:
                db.execute("INSERT INTO storage_headroom_removals(volume_id,removed_at,segments,bytes,newest_removed_at) VALUES(?,?,?,?,?)",
                           (volume_id, now.isoformat(), removed_segments, removed_bytes, newest))
        _record_check(volume_id, now, max(0, wanted - free))
        _say_shortfall(volume_id, volumes, now, max(0, wanted - free), wanted)
        with connect() as db:
            old = ((now - timedelta(days=KEEP_LOG_DAYS)).isoformat(),)
            if has_row(db, "SELECT 1 FROM storage_headroom_removals WHERE removed_at<? LIMIT 1", old):  # Read first (#280, #353)
                db.execute("DELETE FROM storage_headroom_removals WHERE removed_at<?", old)
        results[volume_id] = {"removed_segments": removed_segments, "removed_bytes": removed_bytes, "short_bytes": max(0, wanted - free)}
    return results


def status(db: sqlite3.Connection, volume_id: str, capacity_bytes: int | None, free_bytes: int | None, volumes: dict[str, str], now: datetime | None = None) -> dict[str, object]:
    """What the settings page shows beside a volume's headroom setting."""
    from .retention import effective_retention

    now = now or datetime.now(timezone.utc)
    wanted = effective_headroom(db, volume_id, capacity_bytes)
    row = db.execute("SELECT last_checked_at,short_bytes FROM storage_headroom WHERE volume_id=?", (volume_id,)).fetchone()
    day_ago = (now - timedelta(hours=24)).isoformat()
    recent = (now - timedelta(seconds=RECENT_SECONDS)).isoformat()
    totals = db.execute("SELECT COALESCE(SUM(segments),0) AS segments,COALESCE(SUM(bytes),0) AS bytes FROM storage_headroom_removals WHERE volume_id=? AND removed_at>=?", (volume_id, day_ago)).fetchone()
    removing = db.execute("SELECT 1 FROM storage_headroom_removals WHERE volume_id=? AND removed_at>=? LIMIT 1", (volume_id, recent)).fetchone() is not None
    oldest = None
    days_policy = 0
    cameras = db.execute("SELECT c.id,g.storage_volume_id FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id").fetchall()
    on_volume = [camera["id"] for camera in cameras if (camera["storage_volume_id"] or "default") == volume_id]
    for camera_id in on_volume:
        days_policy = max(days_policy, effective_retention(db, camera_id)[0])
    if on_volume:
        marks = ",".join("?" for _ in on_volume)
        first = db.execute(f"SELECT MIN(starts_at) AS first FROM recording_segments WHERE camera_id IN ({marks}) AND tier_id IS NULL", on_volume).fetchone()
        oldest = first["first"] if first else None
    kept_days = None
    if oldest:
        try:
            started = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            kept_days = round((now - (started if started.tzinfo else started.replace(tzinfo=timezone.utc))).total_seconds() / 86400, 1)
        except ValueError:
            kept_days = None
    return {
        "headroom_bytes": wanted,
        "headroom_is_default": chosen_headroom(db, volume_id) is None,
        "headroom_default_bytes": default_headroom(capacity_bytes),
        "below_headroom": free_bytes is not None and free_bytes < wanted,
        "removing_oldest_footage": removing,
        "removed_24h_segments": int(totals["segments"]),
        "removed_24h_bytes": int(totals["bytes"]),
        "short_bytes": int(row["short_bytes"]) if row else 0,
        "last_checked_at": row["last_checked_at"] if row else None,
        "oldest_footage_days": kept_days,
        "longest_policy_days": days_policy or None,
    }
