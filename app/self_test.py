"""The nightly self-test, and the weekly coverage report's schedule.

Each night the self-test checks that every camera expected to record wrote footage in the
last day, that each camera's signed footage chain still matches its last signature, every
recording volume is writable, and the latest configuration backup is fresh. Each failed check raises its own incident, which recovers when a later run passes;
an administrator can run it at any time to confirm a fix.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Any

from .incidents import sync_incident
from .store import connect
from app.clock import now as _now

logger = logging.getLogger(__name__)

SCHEDULE_TIME = time(3, 41)  # After the nightly backup at 02:17, so its result is fresh.
SILENT_AFTER = timedelta(days=1)
FAULT_TYPE = "self_test"


def _result(check: str, kind: str, name: str, passed: bool | None, detail: str, **extra: Any) -> dict[str, Any]:
    """One check's outcome. ``passed`` is None when the check does not apply (it neither passes nor fails)."""
    return {"check": check, "kind": kind, "name": name, "passed": passed, "detail": detail, **extra}


def _check_cameras(now: datetime) -> list[dict[str, Any]]:
    """Cameras that were expected to record at some point in the last day, and whether they wrote footage."""
    since = (now - SILENT_AFTER).isoformat()
    with connect() as db:
        rows = db.execute(
            "SELECT c.id, c.name, (SELECT MAX(ends_at) FROM recording_segments s WHERE s.camera_id=c.id) AS last_footage "
            "FROM cameras c WHERE c.archived_at IS NULL AND c.enabled=1 AND c.recording_enabled=1 AND EXISTS ("
            "SELECT 1 FROM coverage_intervals i WHERE i.camera_id=c.id AND i.coverage_kind!='expected_idle' AND (i.ends_at IS NULL OR i.ends_at>?)) "
            "ORDER BY c.name COLLATE NOCASE, c.id", (since,)).fetchall()
    results = []
    for row in rows:
        passed = row["last_footage"] is not None and row["last_footage"] > since
        results.append(_result(f"camera:{row['id']}", "camera", row["name"], passed,
                               f"{row['name']}: last footage {row['last_footage']}" if passed else
                               f"{row['name']} has written no footage in the last day" + (f" (last footage {row['last_footage']})" if row["last_footage"] else ""),
                               camera_id=row["id"]))
    return results


def _check_footage_chains() -> list[dict[str, Any]]:
    """Each camera's signed footage chain, from its last signature to its head, still extends that signature intact (#507). The background
    worker checks the same before each signing and records a break; this finds one even while that worker is not running."""
    from . import footage_chain

    results = []
    with connect() as db:
        names = {row["id"]: row["name"] for row in db.execute("SELECT id,name FROM cameras")}
        for head in footage_chain._heads(db):
            camera_id = head["camera_id"]
            name = names.get(camera_id, camera_id)
            fault = footage_chain.chain_fault(db, camera_id)
            results.append(_result(f"footage_chain:{camera_id}", "footage_chain", name, fault is None,
                                   f"{name}: the signed footage chain is intact since its last signature" if fault is None else
                                   f"{name}: the signed footage chain no longer matches its signature at link {fault[0]} ({fault[1]})",
                                   camera_id=camera_id if camera_id in names else None))
    return results


def _check_volumes() -> list[dict[str, Any]]:
    from .storage import storage_status

    return [_result(f"volume:{volume['id']}", "volume", volume["id"], bool(volume["writable"]),
                    f"Recording volume {volume['id']} is writable" if volume["writable"] else f"Recording volume {volume['id']} is not writable ({volume['state']})")
            for volume in storage_status()["volumes"]]


def _check_backup() -> list[dict[str, Any]]:
    from . import backup

    state = backup.status()
    if not state["enabled"]:
        return [_result("backup", "backup", "Configuration backup", None, f"Configuration backups are not configured: {state['disabled_reason']}")]
    if state["age_hours"] is None:
        # A new installation has not reached its first nightly backup yet: only too long without one is a failure.
        with connect() as db:
            first = db.execute("SELECT MIN(created_at) FROM backup_runs").fetchone()[0]
        overdue = first is not None and _now() - datetime.fromisoformat(first.replace("Z", "+00:00")) > backup.STALE_AFTER
        return [_result("backup", "backup", "Configuration backup", False if overdue else None,
                        "No configuration backup has succeeded yet" + ("" if overdue else "; the first one runs tonight"))]
    if state["stale"]:
        return [_result("backup", "backup", "Configuration backup", False, f"The latest configuration backup is {state['age_hours']:.0f} hours old")]
    return [_result("backup", "backup", "Configuration backup", True, f"The latest configuration backup is {state['age_hours']:.1f} hours old")]


def _check_database(full: bool) -> dict[str, Any]:
    """Is the database file damaged? The nightly run reads every page (minutes on a large file); a run by hand only probes it (milliseconds)."""
    from . import db_health

    outcome = db_health.check(full=full)
    what = "passed its integrity check" if full else "opened and read normally"
    if outcome.ok:
        return _result("database", "database", "Database", True, f"The database file {what} ({outcome.seconds:g} s)")
    return _result("database", "database", "Database", False, f"The database file is damaged ({outcome.summary}). {db_health.ADVICE}", lines=outcome.lines)


def _record_damaged(kind: str, started: datetime, database: dict[str, Any]) -> dict[str, Any]:
    """A damaged file ends the run at once: the other checks read the same file, and recovering their incidents on a run that skipped them would be wrong.
    The incident and the run are written if the file still allows it; the log line and the returned result stand either way."""
    finished = _now()
    _announce_damage(database, "self-test")
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                _damage_incident(db, database)
                db.execute("INSERT INTO self_test_runs(kind,started_at,finished_at,passed,results_json) VALUES(?,?,?,0,?)",
                           (kind, started.isoformat(), finished.isoformat(), json.dumps([database])))
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
    except sqlite3.Error:
        logger.exception("The damaged-database result could not be written to the database itself")
    return {"kind": kind, "started_at": started.isoformat(), "finished_at": finished.isoformat(), "passed": False, "results": [database]}


def _announce_damage(database: dict[str, Any], where: str) -> None:
    """One log line, and the record that fails the health check from now on (#453)."""
    from . import db_health

    logger.error("Self-test: %s", database["detail"])
    db_health.report_damage(sqlite3.DatabaseError("; ".join(database.get("lines", [])) or "the integrity check failed"), where)


def _damage_incident(db: sqlite3.Connection, database: dict[str, Any]) -> None:
    sync_incident(db, incident_key=f"{FAULT_TYPE}:database", camera_id=None, fault_type=FAULT_TYPE,
                  summary=f"Self-test failed: {database['detail']}", active=True, details={"check": "database", "detail": database["detail"], "lines": database.get("lines", [])})


_patrol = None


def patrol() -> bool:
    """Every few minutes: check the structure of the database's hot tables, and of the others in turn (#455). Damage is logged once, fails
    the health check and raises the same incident as the nightly check, which is also what recovers it. Returns False when damage was found."""
    from . import db_health

    global _patrol
    if _patrol is None:
        _patrol = db_health.Patrol()
    if not _patrol.due() or db_health.damage_reported():
        return True  # Already known: nothing to add until the NVR is started again on a restored file
    outcome = _patrol.run()
    if outcome.ok:
        return True
    database = _result("database", "database", "Database", False, f"The database file is damaged ({outcome.summary}). {db_health.ADVICE}", lines=outcome.lines)
    _announce_damage(database, "database patrol")
    try:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                _damage_incident(db, database)
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
    except sqlite3.Error:
        logger.exception("The damaged-database incident could not be written to the database itself")
    return False


def run(kind: str = "manual") -> dict[str, Any]:
    """Run every check, record the run, and raise or recover an incident for each check."""
    started = _now()
    database = _check_database(full=kind == "scheduled")  # First, and before the run is recorded: a damaged file cannot be relied on to record anything
    if database["passed"] is False:
        return _record_damaged(kind, started, database)
    with connect() as db:
        run_id = db.execute("INSERT INTO self_test_runs(kind,started_at) VALUES(?,?)", (kind, started.isoformat())).lastrowid
    try:
        results = [database] + _check_cameras(started) + _check_footage_chains() + _check_volumes() + _check_backup()
    except BaseException:
        with connect() as db:  # A run that never finished must not count as tonight's, or it would not be retried.
            db.execute("DELETE FROM self_test_runs WHERE id=?", (run_id,))
        raise
    passed = all(result["passed"] is not False for result in results)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            observed = set()
            for result in results:
                key = f"{FAULT_TYPE}:{result['check']}"
                observed.add(key)
                if result["passed"] is None:
                    continue
                sync_incident(db, incident_key=key, camera_id=result.get("camera_id"), fault_type=FAULT_TYPE,
                              summary=f"Self-test failed: {result['detail']}", active=not result["passed"], details={"check": result["check"], "detail": result["detail"]})
            # A check that no longer exists (a removed camera or volume) has nothing left to fail.
            for row in db.execute("SELECT incident_key FROM incidents WHERE fault_type=? AND fault_active=1", (FAULT_TYPE,)).fetchall():
                if row["incident_key"] not in observed:
                    sync_incident(db, incident_key=row["incident_key"], camera_id=None, fault_type=FAULT_TYPE,
                                  summary="Self-test check no longer applies", active=False, details={})
            db.execute("UPDATE self_test_runs SET finished_at=?,passed=?,results_json=? WHERE id=?",
                       (_now().isoformat(), int(passed), json.dumps(results), run_id))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return latest()  # type: ignore[return-value]


def latest() -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM self_test_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return {"id": row["id"], "kind": row["kind"], "started_at": row["started_at"], "finished_at": row["finished_at"],
            "passed": bool(row["passed"]), "results": json.loads(row["results_json"])}


def due(now: datetime | None = None) -> bool:
    """Whether tonight's scheduled run has not started yet."""
    from .backup import site_zone

    now = now or _now()
    zone = site_zone()
    local = now.astimezone(zone)
    boundary = datetime.combine(local.date(), SCHEDULE_TIME, zone)
    if local < boundary:
        boundary -= timedelta(days=1)
    with connect() as db:
        return db.execute("SELECT 1 FROM self_test_runs WHERE kind='scheduled' AND finished_at IS NOT NULL AND started_at>=? LIMIT 1",
                          (boundary.astimezone(timezone.utc).isoformat(),)).fetchone() is None


class SelfTestManager:
    """Runs the nightly self-test and publishes the weekly coverage report, in the background worker."""

    def __init__(self, poll_seconds: float = 60.0) -> None:
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="self-test")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    @staticmethod
    def process_once() -> None:
        from .coverage import publish_weekly_report

        if not patrol():
            return  # The file is damaged: the full check would only take minutes to say the same
        if due():
            run("scheduled")
            from . import db_health, db_size
            from .store import shrink_log

            if not db_health.damage_reported():
                db_size.measure()  # Which tables hold the space (#512): every page was just read by the check, so this is the cheap moment

            before, after = shrink_log()  # The check held the log open for as long as it read (#493)
            logger.info("The database's write-ahead log was %.1f MB after the nightly check and is %.1f MB now", before / (1 << 20), after / (1 << 20))
        publish_weekly_report()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.process_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - tried again next poll
                from . import db_health

                if db_health.is_damage(exc):
                    db_health.report_damage(exc, "self-test")
                    logger.error("The self-test cannot read the database: the file is damaged (%s). %s", exc, db_health.ADVICE)  # One line, not a traceback a minute
                else:
                    logger.exception("Self-test cycle failed")
            await asyncio.sleep(self.poll_seconds)
