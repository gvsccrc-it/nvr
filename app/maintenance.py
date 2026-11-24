"""Maintenance windows (#234): while a camera, a group of cameras or the whole site is being worked on, nothing is sent out about it.

What a window quiets: a rule's alarm still happens and is kept (marked with the window) but sends no push, no Live pop-in, no webhook and runs none of the rule's actions;
an incident (camera offline, tamper, ...) raises no webhook. Recording goes on and the state of the camera is still shown. An incident that is still there when the window ends is
announced then, so a camera left broken does not stay silent. Windows are one-off: a start and an end, and an end that cannot be more than a week away.

The person who opened a window is reminded shortly before it ends (``remind_due``, on the background worker's push loop), so they can extend it
(``extend``) if the work is not finished. AI analysis of a camera in a window is paused for as long as the window covers it (``paused_cameras``,
read by the AI worker's 5-second pass); the paused time is recorded as not analysed, with the window's name as the reason.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from app.clock import now as _now, stamp as _stamp
from app.store import connect, has_row

MAX_ADMIN_LENGTH = timedelta(days=7)
MAX_OPERATOR_LENGTH = timedelta(hours=8)
MAX_OPERATOR_AHEAD = timedelta(hours=24)  # How far in the future an operator may schedule one to start
MAX_AHEAD = timedelta(days=30)
MIN_LENGTH = timedelta(minutes=1)
MAX_CAMERAS = 200
# The reminder before a window ends: ten minutes, enough to finish up or extend, and for a short window a quarter of its length, so a
# 15-minute window is not "ending" from its fifth minute. A window shorter than four minutes gets none: a reminder under a minute is no use.
REMIND_BEFORE = timedelta(minutes=10)
REMIND_SHARE = 4
MIN_REMIND = timedelta(minutes=1)
MAX_EXTENSION = timedelta(days=7)


class WindowError(ValueError):
    """A window cannot be made or changed as asked."""


class WindowEnded(WindowError):
    """The window has already ended: there is nothing left to change."""


def iso(moment: datetime) -> str:
    """One text form for every stored time, so they compare as text."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _scope_matches(db: sqlite3.Connection, row: sqlite3.Row, camera_id: str | None) -> bool:
    if row["scope"] == "site":
        return True
    if camera_id is None:
        return False  # A fault that belongs to no camera is quieted only by a window for the whole site
    if row["scope"] == "cameras":
        return camera_id in json.loads(row["camera_ids_json"])
    group = db.execute("SELECT group_id FROM cameras WHERE id=?", (camera_id,)).fetchone()
    return group is not None and group["group_id"] is not None and group["group_id"] in json.loads(row["group_ids_json"])


def covering(db: sqlite3.Connection, camera_id: str | None, at: datetime | None = None) -> sqlite3.Row | None:
    """The window that quiets this camera (or, with no camera, the site) at that moment: the one that ends last, if several do."""
    stamp = iso(at or _now())
    rows = db.execute(
        "SELECT * FROM maintenance_windows WHERE starts_at<=? AND COALESCE(ended_at,ends_at)>? ORDER BY COALESCE(ended_at,ends_at) DESC", (stamp, stamp)).fetchall()
    return next((row for row in rows if _scope_matches(db, row, camera_id)), None)


def covered_cameras(db: sqlite3.Connection, at: datetime | None = None) -> set[str]:
    """Every camera a window quiets at that moment, worked out from the active windows at once: what ``covering`` says camera by camera, without
    a query or two for each camera (the maintenance list asked it of every camera on every refresh, #508)."""
    stamp = iso(at or _now())
    rows = db.execute("SELECT scope,camera_ids_json,group_ids_json FROM maintenance_windows WHERE starts_at<=? AND COALESCE(ended_at,ends_at)>?", (stamp, stamp)).fetchall()
    if any(row["scope"] == "site" for row in rows):
        return {row["id"] for row in db.execute("SELECT id FROM cameras")}
    cameras = {camera_id for row in rows if row["scope"] == "cameras" for camera_id in json.loads(row["camera_ids_json"])}
    groups = {group_id for row in rows if row["scope"] == "groups" for group_id in json.loads(row["group_ids_json"])}
    if groups:
        cameras |= {row["id"] for row in db.execute(f"SELECT id FROM cameras WHERE group_id IN ({','.join('?' * len(groups))})", tuple(groups))}
    return cameras


def paused_cameras(db: sqlite3.Connection, at: datetime | None = None) -> dict[str, str]:
    """Each camera a window covers at that moment, with that window's name (the one that ends last, as ``covering`` chooses): the cameras
    whose AI analysis is paused. A constant number of queries however many cameras there are, like ``covered_cameras``."""
    stamp = iso(at or _now())
    rows = db.execute("SELECT name,scope,camera_ids_json,group_ids_json FROM maintenance_windows WHERE starts_at<=? AND COALESCE(ended_at,ends_at)>? "
                      "ORDER BY COALESCE(ended_at,ends_at)", (stamp, stamp)).fetchall()
    if not rows:
        return {}
    every: list[str] | None = None
    groups: dict[str, list[str]] = {}
    wanted = {group_id for row in rows if row["scope"] == "groups" for group_id in json.loads(row["group_ids_json"])}
    if wanted:
        for camera in db.execute(f"SELECT id,group_id FROM cameras WHERE group_id IN ({','.join('?' * len(wanted))})", tuple(wanted)):
            groups.setdefault(camera["group_id"], []).append(camera["id"])
    paused: dict[str, str] = {}
    for row in rows:  # The window that ends last is applied last, so its name is the one kept
        if row["scope"] == "site":
            every = every if every is not None else [camera["id"] for camera in db.execute("SELECT id FROM cameras")]
            cameras = every
        elif row["scope"] == "cameras":
            cameras = json.loads(row["camera_ids_json"])
        else:
            cameras = [camera_id for group_id in json.loads(row["group_ids_json"]) for camera_id in groups.get(group_id, [])]
        paused.update(dict.fromkeys(cameras, row["name"]))
    return paused


def count_held(db: sqlite3.Connection, window_id: str, kind: str) -> None:
    column = "suppressed_alarms" if kind == "alarm" else "suppressed_incidents"
    db.execute(f"UPDATE maintenance_windows SET {column}={column}+1 WHERE id=?", (window_id,))


def state_of(row: sqlite3.Row, at: datetime | None = None) -> str:
    stamp = iso(at or _now())
    if row["starts_at"] > stamp:
        return "upcoming"
    return "active" if (row["ended_at"] or row["ends_at"]) > stamp else "ended"


def reminder_time(starts_at: datetime, ends_at: datetime) -> str | None:
    """When the person who opened a window is reminded that it is about to end (see ``REMIND_BEFORE``), or None for no reminder."""
    lead = min(REMIND_BEFORE, (ends_at - starts_at) / REMIND_SHARE)
    return None if lead < MIN_REMIND else iso(ends_at - lead)


def ending_soon(row: sqlite3.Row, at: datetime | None = None) -> bool:
    """Whether a running window is inside its reminder time: shown beside it, with a way to extend it."""
    return state_of(row, at) == "active" and row["ended_at"] is None and row["remind_at"] is not None and row["remind_at"] <= iso(at or _now())


def view(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    camera_ids = json.loads(row["camera_ids_json"])
    group_ids = json.loads(row["group_ids_json"])
    names = {item["id"]: item["name"] for item in db.execute(f"SELECT id,name FROM cameras WHERE id IN ({','.join('?' for _ in camera_ids)})", camera_ids)} if camera_ids else {}
    group_names = {item["id"]: item["name"] for item in db.execute(f"SELECT id,name FROM camera_groups WHERE id IN ({','.join('?' for _ in group_ids)})", group_ids)} if group_ids else {}
    return {
        "id": row["id"], "name": row["name"], "reason": row["reason"], "scope": row["scope"], "state": state_of(row),
        "camera_ids": camera_ids, "camera_names": [names.get(item, "a removed camera") for item in camera_ids],
        "group_ids": group_ids, "group_names": [group_names.get(item, "a removed group") for item in group_ids],
        "starts_at": row["starts_at"], "ends_at": row["ends_at"], "ended_at": row["ended_at"], "created_by": row["created_by_name"],
        "suppressed_alarms": row["suppressed_alarms"], "suppressed_incidents": row["suppressed_incidents"], "created_at": row["created_at"],
        "ending_soon": ending_soon(row),
    }


def create(
    db: sqlite3.Connection, *, name: str, reason: str, scope: str, camera_ids: list[str], group_ids: list[str], starts_at: datetime, ends_at: datetime,
    actor_id: int, actor_name: str, administrator: bool, can_see: Any,
) -> str:
    """Make a window. Administrators may cover cameras, groups or the site for up to a week; anyone else only cameras they can see, for up to eight hours."""
    now = _now()
    starts_at, ends_at = starts_at.astimezone(timezone.utc), ends_at.astimezone(timezone.utc)
    if ends_at - starts_at < MIN_LENGTH:
        raise WindowError("A maintenance window must last at least a minute")
    if starts_at < now - timedelta(minutes=5):
        raise WindowError("A maintenance window cannot start in the past")
    if starts_at > now + (MAX_AHEAD if administrator else MAX_OPERATOR_AHEAD):
        raise WindowError("That start is too far ahead")
    if ends_at - starts_at > (MAX_ADMIN_LENGTH if administrator else MAX_OPERATOR_LENGTH):
        raise WindowError(f"A maintenance window can last at most {'7 days' if administrator else '8 hours'}: end it and open another if the work goes on")
    if scope not in {"cameras", "groups", "site"}:
        raise WindowError("Choose cameras, groups or the whole site")
    camera_ids, group_ids = list(dict.fromkeys(camera_ids)), list(dict.fromkeys(group_ids))
    if scope != "cameras" and not administrator:
        raise WindowError("Only an administrator can cover groups or the whole site")
    if scope == "cameras":
        if not camera_ids or len(camera_ids) > MAX_CAMERAS:
            raise WindowError("Choose the cameras being worked on")
        group_ids = []
    elif scope == "groups":
        if not group_ids:
            raise WindowError("Choose the groups being worked on")
        camera_ids = []
    else:
        camera_ids, group_ids = [], []
    for camera_id in camera_ids:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None or not can_see(camera_id):
            raise WindowError("Choose cameras you can see")  # The same answer for a camera that does not exist and one that is hidden
    for group_id in group_ids:
        if db.execute("SELECT 1 FROM camera_groups WHERE id=?", (group_id,)).fetchone() is None:
            raise WindowError("Unknown camera group")
    window_id = str(uuid.uuid4())
    stamp = iso(now)
    db.execute(
        "INSERT INTO maintenance_windows(id,name,reason,scope,camera_ids_json,group_ids_json,starts_at,ends_at,created_by,created_by_name,created_at,updated_at,remind_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (window_id, name.strip(), reason.strip(), scope, json.dumps(camera_ids), json.dumps(group_ids), iso(starts_at), iso(ends_at), actor_id, actor_name, stamp, stamp,
         reminder_time(starts_at, ends_at)))
    return window_id


def end_now(db: sqlite3.Connection, window_id: str) -> sqlite3.Row:
    """End a window now (a window that has not started is removed: nothing happened in it). Returns the window as it was."""
    row = db.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone()
    if row is None:
        raise LookupError("Maintenance window not found")
    state = state_of(row)
    if state == "ended":
        raise WindowError("That maintenance window has already ended")
    if state == "upcoming":
        db.execute("DELETE FROM maintenance_windows WHERE id=?", (window_id,))
    else:
        stamp = iso(_now())
        db.execute("UPDATE maintenance_windows SET ended_at=?,updated_at=? WHERE id=?", (stamp, stamp, window_id))
    return row


def extend(db: sqlite3.Connection, window_id: str, minutes: int, *, administrator: bool) -> sqlite3.Row:
    """Move a running (or not yet started) window's end later by that many minutes, within the same limit as opening one: eight hours in
    all for anyone but an administrator, seven days for an administrator. The reminder is set again for the new end; when the new end is
    already inside its reminder time (a few minutes added near the end), the person extending it has just seen it, so none is sent.
    Returns the window as it is now."""
    row = db.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone()
    if row is None:
        raise LookupError("Maintenance window not found")
    if state_of(row) == "ended":
        raise WindowEnded("That maintenance window has already ended")
    if minutes < 1 or timedelta(minutes=minutes) > MAX_EXTENSION:
        raise WindowError("Extend it by at least a minute and at most 7 days")
    now = _now()
    starts_at, ends_at = parse(row["starts_at"]), parse(row["ends_at"]) + timedelta(minutes=minutes)
    if ends_at - starts_at > (MAX_ADMIN_LENGTH if administrator else MAX_OPERATOR_LENGTH):
        raise WindowError(f"A maintenance window can last at most {'7 days' if administrator else '8 hours'}: end it and open another if the work goes on")
    remind_at = reminder_time(starts_at, ends_at) if row["created_by"] is not None else None
    reminded = _stamp(now) if remind_at is not None and remind_at <= iso(now) else None
    db.execute("UPDATE maintenance_windows SET ends_at=?,remind_at=?,reminded_at=?,updated_at=? WHERE id=?", (iso(ends_at), remind_at, reminded, iso(now), window_id))
    return db.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone()


_DUE = "FROM maintenance_windows WHERE reminded_at IS NULL AND ended_at IS NULL AND remind_at<=? AND ends_at>? AND created_by IS NOT NULL"


def remind_due(at: datetime | None = None) -> list[str]:
    """Remind the person who opened each window now inside its reminder time, once: a push notification to their devices (the window also
    shows as ending soon in the status bar). Run by the background worker every couple of seconds: it reads first and takes the write lock
    only when a reminder is due (#280). Returns the windows reminded."""
    moment = at or _now()
    values = (iso(moment), iso(moment))
    with connect() as db:
        if not has_row(db, f"SELECT 1 {_DUE} LIMIT 1", values):
            return []
        from . import push

        db.execute("BEGIN IMMEDIATE")
        try:
            reminded = []
            for row in db.execute(f"SELECT * {_DUE}", values).fetchall():
                db.execute("UPDATE maintenance_windows SET reminded_at=? WHERE id=?", (_stamp(moment), row["id"]))
                push.queue_maintenance_reminder(db, row, moment)
                reminded.append(row["id"])
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return reminded
