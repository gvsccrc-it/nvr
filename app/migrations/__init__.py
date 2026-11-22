"""Versioned, forward-only schema migrations.

Each migration module exposes ``NAME`` and ``upgrade(db)``. Migrations run inside a
transaction together with their version record unless the module sets
``TRANSACTIONAL = False`` (the baseline manages its own transactions). A module may
also expose ``verify(db)`` to refuse a result whose shape is not what it expects.

Migrations never run backwards. A database recorded at a version newer than this
release knows about is refused, because an older image cannot safely use a schema
it did not create.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable

from app.migrations import m0001_baseline, m0002_api_tokens, m0003_worker_status, m0004_backups, m0005_user_preferences, m0006_export_retries, m0007_detections, m0008_rules, m0009_alarms, m0010_push, m0011_live_alarms, m0012_rule_actions, m0013_ptz, m0014_privacy_masks, m0015_footage_chain, m0016_coverage_reasons, m0017_second_factors, m0018_access_grants, m0019_viewing_audit, m0020_metrics, m0021_tamper, m0022_disk_health, m0023_hardening, m0024_camera_capabilities, m0025_track_attributes, m0026_track_embeddings, m0027_track_image_embeddings, m0028_cases, m0029_redaction, m0030_edge_backfill, m0031_remote_second_factor, m0032_digests, m0033_camera_health, m0034_external_events, m0035_retention_purpose, m0036_native_app, m0037_console, m0038_plates, m0039_failover, m0040_tiering, m0041_ptz_tracking, m0042_fisheye, m0043_two_way_audio, m0044_updates, m0045_plate_region, m0046_storage_headroom, m0047_falls, m0048_email_alerts, m0049_maintenance, m0050_incident_emails, m0051_ai_capacity, m0052_email_error_detail, m0053_vehicle_threshold, m0054_pose_points, m0055_inactivity_rules, m0056_occupancy_rules, m0057_live_ready, m0058_face_watch, m0059_face_sightings, m0060_revoke_device_sessions, m0061_session_device, m0062_face_provenance, m0063_package_provenance, m0064_camera_add_jobs, m0065_motion_event_indexes, m0066_ai_observation_indexes, m0067_track_removed_trigger, m0068_webhook_delivery_indexes, m0069_account_disabled, m0070_security_log, m0071_ai_coverage_indexes, m0072_export_package_hash, m0073_failover_yield_since, m0074_viewing_audit_open_index, m0075_alarm_history, m0076_export_retention, m0077_camera_add_candidates, m0078_volume_markers, m0079_cascade_indexes, m0080_push_services, m0081_coverage_indexes, m0082_footage_chain_checkpoints, m0083_update_builds, m0084_track_picture_files, m0085_detection_thinning, m0086_occupancy_stretches, m0087_alarm_ceiling, m0088_operator_hold_expiry, m0089_passkey_origin, m0090_no_address_in_onvif_errors, m0091_talk_unprotected_sign_in, m0092_forecast_snapshot, m0093_viewing_audit_playback_index, m0094_hardening_run_progress, m0095_incident_history

logger = logging.getLogger("nvr.migrations")

MIGRATIONS: list[tuple[int, ModuleType]] = [
    (1, m0001_baseline),
    (2, m0002_api_tokens),
    (3, m0003_worker_status),
    (4, m0004_backups),
    (5, m0005_user_preferences),
    (6, m0006_export_retries),
    (7, m0007_detections),
    (8, m0008_rules),
    (9, m0009_alarms),
    (10, m0010_push),
    (11, m0011_live_alarms),
    (12, m0012_rule_actions),
    (13, m0013_ptz),
    (14, m0014_privacy_masks),
    (15, m0015_footage_chain),
    (16, m0016_coverage_reasons),
    (17, m0017_second_factors),
    (18, m0018_access_grants),
    (19, m0019_viewing_audit),
    (20, m0020_metrics),
    (21, m0021_tamper),
    (22, m0022_disk_health),
    (23, m0023_hardening),
    (24, m0024_camera_capabilities),
    (25, m0025_track_attributes),
    (26, m0026_track_embeddings),
    (27, m0027_track_image_embeddings),
    (28, m0028_cases),
    (29, m0029_redaction),
    (30, m0030_edge_backfill),
    (31, m0031_remote_second_factor),
    (32, m0032_digests),
    (33, m0033_camera_health),
    (34, m0034_external_events),
    (35, m0035_retention_purpose),
    (36, m0036_native_app),
    (37, m0037_console),
    (38, m0038_plates),
    (39, m0039_failover),
    (40, m0040_tiering),
    (41, m0041_ptz_tracking),
    (42, m0042_fisheye),
    (43, m0043_two_way_audio),
    (44, m0044_updates),
    (45, m0045_plate_region),
    (46, m0046_storage_headroom),
    (47, m0047_falls),
    (48, m0048_email_alerts),
    (49, m0049_maintenance),
    (50, m0050_incident_emails),
    (51, m0051_ai_capacity),
    (52, m0052_email_error_detail),
    (53, m0053_vehicle_threshold),
    (54, m0054_pose_points),
    (55, m0055_inactivity_rules),
    (56, m0056_occupancy_rules),
    (57, m0057_live_ready),
    (58, m0058_face_watch),
    (59, m0059_face_sightings),
    (60, m0060_revoke_device_sessions),
    (61, m0061_session_device),
    (62, m0062_face_provenance),
    (63, m0063_package_provenance),
    (64, m0064_camera_add_jobs),
    (65, m0065_motion_event_indexes),
    (66, m0066_ai_observation_indexes),
    (67, m0067_track_removed_trigger),
    (68, m0068_webhook_delivery_indexes),
    (69, m0069_account_disabled),
    (70, m0070_security_log),
    (71, m0071_ai_coverage_indexes),
    (72, m0072_export_package_hash),
    (73, m0073_failover_yield_since),
    (74, m0074_viewing_audit_open_index),
    (75, m0075_alarm_history),
    (76, m0076_export_retention),
    (77, m0077_camera_add_candidates),
    (78, m0078_volume_markers),
    (79, m0079_cascade_indexes),
    (80, m0080_push_services),
    (81, m0081_coverage_indexes),
    (82, m0082_footage_chain_checkpoints),
    (83, m0083_update_builds),
    (84, m0084_track_picture_files),
    (85, m0085_detection_thinning),
    (86, m0086_occupancy_stretches),
    (87, m0087_alarm_ceiling),
    (88, m0088_operator_hold_expiry),
    (89, m0089_passkey_origin),
    (90, m0090_no_address_in_onvif_errors),
    (91, m0091_talk_unprotected_sign_in),
    (92, m0092_forecast_snapshot),
    (93, m0093_viewing_audit_playback_index),
    (94, m0094_hardening_run_progress),
    (95, m0095_incident_history),
]

PRE_MIGRATION_COPIES_KEPT = 3
# Beside those, the newest copy at least this much older than the newest of all is kept too: damage found late must not leave only copies
# made after it. On the reporting site all three copies were two days old and hours from being replaced by copies of the damaged file (#455).
OLDER_COPY_SECONDS = 24 * 3600.0
DAMAGED_COPIES_KEPT = 1  # Copies that failed their check, kept under another name for salvage only
STEP_PROGRESS_SECONDS = 30.0  # A long migration step says it is still running this often

# Called with the database path before pending migrations run on a database that
# holds data. Configuration backups register here; a failing hook is logged and the
# local pre-migration copy remains the guarantee.
pre_migration_hooks: list[Callable[[Path], None]] = []


class MigrationError(RuntimeError):
    pass


def latest_version() -> int:
    return MIGRATIONS[-1][0]


def migrate(path: Path, connect: Callable[[], sqlite3.Connection]) -> list[int]:
    """Apply pending migrations and return the versions applied."""
    with connect() as db:
        tables = _user_tables(db)
        applied = _applied_versions(db) if "schema_migrations" in tables else set()
        known = {version for version, _ in MIGRATIONS}
        unknown = sorted(applied - known)
        if unknown:
            raise MigrationError(
                f"The database at {path} was migrated by a newer release (schema version {unknown[-1]}); "
                f"this release supports up to version {latest_version()}. Restore the pre-upgrade "
                "configuration backup or run the newer release."
            )
        pending = [(version, module) for version, module in MIGRATIONS if version not in applied]
        if not pending:
            db.execute("PRAGMA journal_mode = WAL")
            return []

        holds_data = bool(tables - {"schema_migrations"})
        if holds_data and "schema_migrations" not in tables and "users" not in tables:
            raise MigrationError(
                f"The database at {path} is not a recognized NVR database; refusing to modify it."
            )
        copy_path = None
        if holds_data:
            # Both copies take time in proportion to the database's size, and the NVR answers nothing until they are done: say how long (#418).
            started = time.monotonic()
            copy_path, fresh = _copy_for(db, path, max(applied, default=0), pending[-1][0])
            logger.info("The pre-migration copy took %.1f s", time.monotonic() - started)
            for hook in pre_migration_hooks if fresh else []:
                started = time.monotonic()
                try:
                    hook(path)
                except Exception:  # noqa: BLE001 - the local copy still protects the upgrade
                    logger.exception("Pre-migration hook failed; continuing with the local copy at %s", copy_path)
                else:
                    logger.info("The pre-migration hook %s took %.1f s", getattr(hook, "__name__", hook), time.monotonic() - started)

        db.execute("PRAGMA journal_mode = WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        done: list[int] = []
        for version, module in pending:
            try:
                _apply(db, version, module)
            except Exception as exc:
                where = f"; the pre-migration copy is {copy_path}" if copy_path else ""
                if copy_path and not done:  # The file is as the copy left it: the next start need not copy it again
                    _remember_attempt(path, max(applied, default=0), pending[-1][0], copy_path)
                raise MigrationError(
                    f"Schema migration {version} ({module.NAME}) failed: {exc}{where}"
                ) from exc
            logger.info("Applied schema migration %s (%s)", version, module.NAME)
            done.append(version)
        _attempt_file(path).unlink(missing_ok=True)
        return done


def _apply(db: sqlite3.Connection, version: int, module: ModuleType) -> None:
    """One step, saying every half minute that it is still running: a slow index build on a large database is not a hang (#529)."""
    started = time.monotonic()
    said = {"at": started}

    def progress() -> int:
        now = time.monotonic()
        if now - said["at"] >= STEP_PROGRESS_SECONDS:
            said["at"] = now
            logger.info("Schema migration %s (%s) is still running after %.0f s", version, module.NAME, now - started)
        return 0

    db.set_progress_handler(progress, 100_000)
    try:
        _apply_step(db, version, module)
    finally:
        db.set_progress_handler(None, 0)


def _apply_step(db: sqlite3.Connection, version: int, module: ModuleType) -> None:
    record = "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)"
    values = (version, module.NAME, _now())
    if getattr(module, "TRANSACTIONAL", True):
        db.execute("BEGIN IMMEDIATE")
        try:
            module.upgrade(db)
            if hasattr(module, "verify"):
                module.verify(db)
            db.execute(record, values)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return
    module.upgrade(db)
    if hasattr(module, "verify"):
        module.verify(db)
    db.execute(record, values)


def _user_tables(db: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }


def _applied_versions(db: sqlite3.Connection) -> set[int]:
    return {row[0] for row in db.execute("SELECT version FROM schema_migrations")}


SPACE_MARGIN = 64 << 20  # Left free after the copy, for the migration itself


def _copies_of(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.pre-migration-*"), key=lambda item: item.stat().st_mtime)


def _database_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in (path, path.with_name(path.name + "-wal")) if item.exists())


def _make_room(path: Path, needed: int) -> int:
    """Free space for the copy by removing older copies (oldest first) only as far as needed: a full disk must not stop the NVR starting. Returns the free space."""
    free = shutil.disk_usage(path.parent).free
    older = _copies_of(path)
    while free < needed and older:
        stale = older.pop(0)
        size = stale.stat().st_size
        stale.unlink(missing_ok=True)
        logger.warning("The disk is nearly full: removed the older pre-migration copy %s (%d bytes) to make room", stale.name, size)
        free = shutil.disk_usage(path.parent).free
    return free


def _attempt_file(path: Path) -> Path:
    return path.with_name(f".{path.name}.migration-attempt.json")


def _same_file(path: Path) -> list[int]:
    status = path.stat()
    return [status.st_dev, status.st_ino]


def _remember_attempt(path: Path, from_version: int, to_version: int, copy_path: Path) -> None:
    """A step failed before anything was applied. The container is restarted and tries again: that start uses this copy, and so do the ones
    after it, so a migration that keeps failing neither fills the disk nor pushes out the copies of earlier versions (#529)."""
    try:
        _attempt_file(path).write_text(json.dumps({"from": from_version, "to": to_version, "copy": copy_path.name, "file": _same_file(path)}), encoding="utf-8")
    except OSError:
        logger.warning("Could not note the failed upgrade; the next start makes a new copy")


def _copy_from_last_attempt(path: Path, from_version: int, to_version: int) -> Path | None:
    try:
        noted = json.loads(_attempt_file(path).read_text(encoding="utf-8"))
        copy_path = path.with_name(str(noted["copy"]))
        if (noted["from"], noted["to"], noted["file"]) == (from_version, to_version, _same_file(path)) and copy_path.parent == path.parent and copy_path.is_file():
            return copy_path
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _damaged(db: sqlite3.Connection) -> str | None:
    """What the light probe (the start of every table) or the record of damage found while running says, or None."""
    from app import db_health

    try:
        db_health._probe(db)
    except sqlite3.DatabaseError as exc:
        if not db_health.is_damage(exc):
            raise
        return str(exc)
    found = db_health.damage_reported()
    return str(found.get("error")) if found else None


def _copy_for(db: sqlite3.Connection, path: Path, from_version: int, to_version: int) -> tuple[Path | None, bool]:
    """The copy this upgrade can be undone with, and whether it was made now. A file known to be damaged is not copied: a copy of it would
    take the place of a healthy one, and the healthy ones are what a recovery needs (#455, #529)."""
    again = _copy_from_last_attempt(path, from_version, to_version)
    if again is not None:
        logger.info("The last start failed at this same upgrade: using the copy it made, %s", again.name)
        return again, False
    damage = _damaged(db)
    if damage is not None:
        logger.error("The database file is damaged (%s): no pre-migration copy was made of it and the older copies are all kept", damage)
        return None, False
    made = _pre_migration_copy(db, path, from_version, to_version)
    return made, made is not None


def _check_copy(target: Path) -> list[str]:
    """Every page of the new copy, read once: the copy is made page by page without looking inside them, so a lost page is copied as it is."""
    check = sqlite3.connect(target)
    try:
        found = [str(row[0]) for row in check.execute("PRAGMA quick_check(5)").fetchall()]
    except sqlite3.DatabaseError as exc:
        found = [str(exc)]
    finally:
        check.close()
    return [] if found == ["ok"] else found


def _set_aside(path: Path, target: Path, found: list[str]) -> None:
    """A copy that failed its check is renamed so that nothing offers it for a restore, and it never counts among the copies kept."""
    from app import db_health

    aside = path.with_name(target.name.replace(".pre-migration-", ".damaged-copy-", 1))
    os.replace(target, aside)
    for stale in sorted(path.parent.glob(f"{path.name}.damaged-copy-*"), key=lambda item: item.stat().st_mtime)[:-DAMAGED_COPIES_KEPT]:
        stale.unlink(missing_ok=True)
    summary = "; ".join(" ".join(line.split()) for line in found)
    logger.error("THE DATABASE FILE IS DAMAGED (%s). The copy made before this upgrade is damaged too: it was renamed %s and the older copies are all kept. %s",
                 summary, aside.name, db_health.ADVICE)
    db_health.report_damage(sqlite3.DatabaseError(summary), "pre-migration copy")


def _prune_copies(path: Path) -> None:
    """Keep the newest few, and the newest of those at least a day older than the newest of all."""
    copies = _copies_of(path)
    if not copies:
        return
    newest = copies[-1].stat().st_mtime
    older = [item for item in copies if newest - item.stat().st_mtime >= OLDER_COPY_SECONDS]
    kept = set(copies[-PRE_MIGRATION_COPIES_KEPT:]) | set(older[-1:])
    for stale in copies:
        if stale not in kept:
            stale.unlink(missing_ok=True)


def _pre_migration_copy(db: sqlite3.Connection, path: Path, from_version: int, to_version: int) -> Path | None:
    needed = _database_bytes(path) + SPACE_MARGIN
    free = _make_room(path, needed)
    if free < needed:
        raise MigrationError(
            f"There is not enough free disk space to upgrade the database safely: {free // (1 << 20)} MB free where {path.parent} is, {needed // (1 << 20)} MB needed "
            "(a copy of the database is kept before every upgrade). Free some space on that disk (for example delete the oldest recordings or other large files, "
            "or remove files named *.pre-migration-* in that folder) and start again. Nothing was changed."
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(f"{path.name}.pre-migration-v{from_version}-to-v{to_version}-{stamp}")
    copy = sqlite3.connect(target)
    try:
        db.backup(copy)
    except BaseException:
        copy.close()
        for leftover in (target, target.with_name(target.name + "-journal")):
            leftover.unlink(missing_ok=True)  # A half-written copy is no use and would fill the disk further on every restart
        raise
    copy.close()
    os.chmod(target, 0o600)
    found = _check_copy(target)
    if found:
        _set_aside(path, target, found)
        return None
    _prune_copies(path)
    logger.info("Saved pre-migration database copy to %s", target)
    return target


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
