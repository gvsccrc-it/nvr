from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


# Process faults that do not stop recording or analysis.
SYSTEM_FAULT_TYPES = {"worker_web", "worker_exporter", "worker_rules", "worker_background", "backup", "self_test", "failover", "failover_sync", "webhook_drops", "footage_chain"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(db: sqlite3.Connection, incident_id: str, event_type: str, details: dict[str, Any], actor_id: int | None = None) -> None:
    event_id = str(uuid.uuid4())
    occurred_at = _now()
    db.execute(
        "INSERT INTO incident_events(event_id,incident_id,event_type,actor_id,occurred_at,details_json) VALUES(?,?,?,?,?,?)",
        (event_id, incident_id, event_type, actor_id, occurred_at, json.dumps(details, sort_keys=True)),
    )
    db.execute("UPDATE incidents SET event_count=event_count+1 WHERE id=?", (incident_id,))  # So the list never counts them (#516)
    if event_type not in {"raised", "recurred", "recovered"}:
        return
    incident = db.execute("SELECT fault_type,camera_id,summary,recurrence_count FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if incident is None:
        return
    from . import maintenance

    window = maintenance.covering(db, incident["camera_id"])
    if window is not None:
        # Worked on at the moment: kept and shown, but nothing goes out. A fault still there when the window ends is announced then (``release_held``).
        maintenance.count_held(db, window["id"], "incident")
        db.execute("UPDATE incidents SET held_by_window=? WHERE id=?", (window["id"] if event_type != "recovered" else None, incident_id))
        return
    from . import email_outbox

    email_outbox.queue_incident(db, db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone(), event_type, occurred_at, details)
    if str(incident["fault_type"]).startswith("ai_"):
        webhook_type = "ai.recovered" if event_type == "recovered" else "ai.failure"
    elif str(incident["fault_type"]).startswith("tamper_"):
        webhook_type = "tamper.recovered" if event_type == "recovered" else "tamper.detected"
    elif incident["fault_type"] in SYSTEM_FAULT_TYPES:
        webhook_type = "system.recovered" if event_type == "recovered" else "system.failure"
    else:
        category = "storage" if incident["fault_type"] in {"storage", "disk_health", "retention_forecast", "tiering"} else "recording"
        if event_type == "recovered":
            webhook_type = f"{category}.recovered"
        elif incident["fault_type"] == "recording_uncertain":
            webhook_type = "recording.uncertain"
        else:
            webhook_type = f"{category}.failure"
    from .webhooks import enqueue_event
    enqueue_event(db, webhook_type, {
        "event_id": event_id,
        "event_type": webhook_type,
        "occurred_at": occurred_at,
        "incident_id": incident_id,
        "fault_type": incident["fault_type"],
        "camera_id": incident["camera_id"],
        "summary": incident["summary"],
        "recurrence_count": int(incident["recurrence_count"]),
        "details": details,
    })


def sync_incident(
    db: sqlite3.Connection,
    *,
    incident_key: str,
    camera_id: str | None,
    fault_type: str,
    summary: str,
    active: bool,
    details: dict[str, Any] | None = None,
) -> None:
    """Persist a fault observation; repeated observations retain stable incident identity."""
    details_json = json.dumps(details or {}, sort_keys=True)
    row = db.execute("SELECT * FROM incidents WHERE incident_key=?", (incident_key,)).fetchone()
    now = _now()
    if active:
        if row is None:
            incident_id = str(uuid.uuid4())
            created = db.execute(
                "INSERT INTO incidents(id,incident_key,camera_id,fault_type,summary,state,fault_active,recurrence_count,first_seen_at,last_seen_at,details_json) "
                "VALUES(?,?,?,?,?,'active',1,1,?,?,?) ON CONFLICT(incident_key) DO NOTHING",
                (incident_id, incident_key, camera_id, fault_type, summary, now, now, details_json),
            )
            if created.rowcount:
                _event(db, incident_id, "raised", details or {})
            else:
                # Another supervisor may have observed the same fault between our read and insert.
                row = db.execute("SELECT * FROM incidents WHERE incident_key=?", (incident_key,)).fetchone()
        elif row["state"] == "recovered" or not row["fault_active"]:
            db.execute(
                "UPDATE incidents SET camera_id=?,fault_type=?,summary=?,state='active',fault_active=1,recurrence_count=recurrence_count+1,last_seen_at=?,acknowledged_by=NULL,acknowledged_at=NULL,recovered_at=NULL,details_json=? WHERE id=?",
                (camera_id, fault_type, summary, now, details_json, row["id"]),
            )
            _event(db, row["id"], "recurred", details or {})
        else:
            db.execute(
                "UPDATE incidents SET summary=?,last_seen_at=?,details_json=? WHERE id=?",
                (summary, now, details_json, row["id"]),
            )
            if row["held_by_window"]:
                release_held(db, row["id"], details or {})
    elif row is not None and row["fault_active"]:
        db.execute(
            "UPDATE incidents SET state='recovered',fault_active=0,last_seen_at=?,recovered_at=?,details_json=? WHERE id=?",
            (now, now, details_json, row["id"]),
        )
        _event(db, row["id"], "recovered", details or {})


def release_held(db: sqlite3.Connection, incident_id: str, details: dict[str, Any]) -> None:
    """An incident whose notification was held back during a maintenance window is announced now if no window covers it any more and the fault is still active."""
    from . import maintenance

    row = db.execute("SELECT camera_id,fault_active,held_by_window FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if row is None or not row["held_by_window"] or maintenance.covering(db, row["camera_id"]) is not None:
        return
    db.execute("UPDATE incidents SET held_by_window=NULL WHERE id=?", (incident_id,))
    if row["fault_active"]:
        _event(db, incident_id, "raised", {**details, "held_during_maintenance": True})


def acknowledge_incident(db: sqlite3.Connection, incident_id: str, actor_id: int) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if row is None:
        return None
    if not row["fault_active"]:
        return {**dict(row), "acknowledgement_conflict": True}
    if row["state"] != "acknowledged":
        now = _now()
        db.execute(
            "UPDATE incidents SET state='acknowledged',acknowledged_by=?,acknowledged_at=? WHERE id=? AND fault_active=1",
            (actor_id, now, incident_id),
        )
        _event(db, incident_id, "acknowledged", {}, actor_id)
    return dict(db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())


EVENTS_SHOWN = 5  # Newest events of each incident in the list; the whole history is on its own route (#516)
HISTORY_PAGE = 500  # Events per page of one incident's history
PRUNE_BATCH = 500  # Rows removed per write transaction, so the pass never holds the write lock for long


def _view(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["fault_active"] = bool(result["fault_active"])
    result["details"] = json.loads(result.pop("details_json"))
    result["events_total"] = int(result.pop("event_count")) + int(result["events_removed"])  # Every event it has had; events_removed are no longer kept
    return result


def _event_view(row: sqlite3.Row) -> dict[str, Any]:
    event = dict(row)
    event["details"] = json.loads(event.pop("details_json"))
    return event


def _with_newest_events(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Each incident with its EVENTS_SHOWN newest events, oldest first, in one statement for the page: an incident whose fault comes and goes
    gained events for ever, and the list sent them all every 15 seconds to every open tab (#516)."""
    views = [_view(row) for row in rows]
    events: dict[str, list[dict[str, Any]]] = {view["id"]: [] for view in views}
    if views:
        for event in db.execute(
                "SELECT e.* FROM incidents i JOIN incident_events e ON e.id IN (SELECT x.id FROM incident_events x WHERE x.incident_id=i.id ORDER BY x.id DESC LIMIT ?) "
                "WHERE i.id IN (SELECT value FROM json_each(?)) ORDER BY e.id", (EVENTS_SHOWN, json.dumps(list(events)))):
            events[event["incident_id"]].append(_event_view(event))
    for view in views:
        view["events"] = events[view["id"]]
    return views


def incident_view(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return _with_newest_events(db, [row])[0]


def list_incidents(db: sqlite3.Connection, state: str | None = None, limit: int = 200, cameras: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """The newest incidents, of one state or all. ``cameras`` limits them to those cameras' incidents (incidents naming no camera are the
    system's, and left out): filtered in the query, so a restricted person's page is read through an index like anyone else's (#516)."""
    where, values = [], []
    if state is not None:
        # With cameras, the unary + keeps the state index out: walking every incident in a state (months of recovered ones) to find a few
        # cameras' would cost more than reading those cameras' incidents through their own index
        where.append("+state=?" if cameras is not None else "state=?")
        values.append(state)
    if cameras is not None:
        if not cameras:
            return []
        where.append("camera_id IN (SELECT value FROM json_each(?))")
        values.append(json.dumps(sorted(cameras)))
    clause = f"WHERE {' AND '.join(where)} " if where else ""
    rows = db.execute(f"SELECT * FROM incidents {clause}ORDER BY last_seen_at DESC LIMIT ?", (*values, limit)).fetchall()
    return _with_newest_events(db, rows)


def history(db: sqlite3.Connection, incident_id: str, before: int | None = None, limit: int = HISTORY_PAGE) -> dict[str, Any] | None:
    """One incident with a page of its history, newest page first and each page oldest first. ``next`` is the ``before`` of the page of
    older events, or None when this page reaches the oldest one kept."""
    row = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if row is None:
        return None
    rows = db.execute("SELECT * FROM incident_events WHERE incident_id=? AND id<? ORDER BY id DESC LIMIT ?",
                      (incident_id, before if before is not None else 2 ** 63 - 1, limit + 1)).fetchall()
    result = _view(row)
    result["events"] = [_event_view(event) for event in reversed(rows[:limit])]
    result["next"] = rows[limit - 1]["id"] if len(rows) > limit else None
    return result


def prune_history(now: datetime | None = None) -> dict[str, int]:
    """Keep incident history for the alarm history period (``alarm_settings.history_days``, #349), as decided on #516.

    An incident that is active or acknowledged is never removed, however old. A recovered one goes, with its events, once it has been
    recovered for longer than the period. Older events of the incidents that stay are removed and counted on the incident, so a fault
    that comes and goes for months cannot grow its history without limit. Each step reads what is due first and takes the write lock
    only when there is something to do (#280), a batch at a time."""
    from datetime import timedelta

    from .alarms import settings
    from .store import connect, has_row

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    done = {"removed_incidents": 0, "removed_events": 0}
    with connect() as db:
        before = (now - timedelta(days=settings(db)["history_days"])).isoformat()  # The form _now writes
        recovered = "SELECT id FROM incidents WHERE state='recovered' AND recovered_at<?"
        while has_row(db, f"{recovered} LIMIT 1", (before,)):
            ids = [row[0] for row in db.execute(f"{recovered} LIMIT ?", (before, PRUNE_BATCH))]
            # Their events first, a batch at a time: one incident can have thousands, and the cascade would remove them in one go
            while has_row(db, "SELECT 1 FROM incident_events WHERE incident_id IN (SELECT value FROM json_each(?)) LIMIT 1", (json.dumps(ids),)):
                _write(db, "DELETE FROM incident_events WHERE id IN (SELECT id FROM incident_events WHERE incident_id IN (SELECT value FROM json_each(?)) LIMIT ?)",
                       (json.dumps(ids), PRUNE_BATCH))
            done["removed_incidents"] += _write(db, "DELETE FROM incidents WHERE id IN (SELECT value FROM json_each(?)) AND state='recovered' AND recovered_at<?",
                                                (json.dumps(ids), before))
        old = "SELECT id,incident_id FROM incident_events WHERE occurred_at<?"
        while has_row(db, f"{old} LIMIT 1", (before,)):
            rows = db.execute(f"{old} ORDER BY occurred_at LIMIT ?", (before, PRUNE_BATCH)).fetchall()
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["incident_id"]] = counts.get(row["incident_id"], 0) + 1
            db.execute("BEGIN IMMEDIATE")
            try:
                removed = db.execute("DELETE FROM incident_events WHERE id IN (SELECT value FROM json_each(?))", (json.dumps([row["id"] for row in rows]),)).rowcount
                db.executemany("UPDATE incidents SET event_count=MAX(event_count-?,0),events_removed=events_removed+?,events_removed_before=? WHERE id=?",
                               [(count, count, before, incident_id) for incident_id, count in counts.items()])
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
            done["removed_events"] += removed
    return done


def _write(db: sqlite3.Connection, statement: str, values: tuple[Any, ...]) -> int:
    db.execute("BEGIN IMMEDIATE")
    try:
        changed = db.execute(statement, values).rowcount
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
    return changed


def sync_storage_incidents(db: sqlite3.Connection, volumes: list[dict[str, Any]]) -> None:
    """Group an unhealthy recording volume into one incident with its affected cameras."""
    observed: set[str] = set()
    unhealthy_states = {"pressure", "missing", "read_only", "unavailable", "slow", "unconfigured", "wrong_disk", "disk_not_mounted"}
    explained = {  # States a person cannot act on from one word (#479)
        "disk_not_mounted": "the disk it recorded to is not mounted (the folder is there but the footage is not). Nothing is recorded to it and none of its footage is removed until it is back: "
                            "check that the disk is connected and mounted, or, if it was replaced on purpose, say so in Settings > Recording & storage",
        "wrong_disk": "the disk mounted there belongs to another NVR. Nothing is recorded to it: mount the right disk, or, if this disk is meant to replace it, say so in Settings > Recording & storage",
    }
    for volume in volumes:
        volume_id = str(volume["id"])
        key = f"storage:{volume_id}"
        observed.add(key)
        affected = volume.get("cameras", [])
        state = str(volume.get("state", "unavailable"))
        sync_incident(
            db,
            incident_key=key,
            camera_id=None,
            fault_type="storage",
            summary=(f"Recording storage {volume_id}: {explained[state]}; {len(affected)} camera(s) are assigned" if state in explained
                     else f"Recording storage {volume_id} is {state}; {len(affected)} camera(s) are assigned"),
            active=state in unhealthy_states and bool(affected),
            details={"volume_id": volume_id, "volume_state": state, "affected_cameras": affected},
        )
    rows = db.execute("SELECT incident_key FROM incidents WHERE fault_type='storage' AND fault_active=1").fetchall()
    for row in rows:
        if row["incident_key"] not in observed:
            sync_incident(
                db,
                incident_key=row["incident_key"],
                camera_id=None,
                fault_type="storage",
                summary="Recording storage is no longer configured",
                active=False,
                details={"volume_state": "removed"},
            )
