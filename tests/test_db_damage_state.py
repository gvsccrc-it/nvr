"""Damage found deeper in the database file than the light probe reads (#453).

On the reporting site four pages of two tables turned to zeros. The probe, which reads the first row of every table, still passed, so the
container stayed "healthy" for 16 hours while the recorder exited on every start (564 times a day) and every loop logged a traceback a pass.
Whoever meets the damage now records it in one file: the health check fails, the worker exits with a code the supervisor answers with a long
wait, and each process logs one clear line."""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db_health
from app.workers import supervisor

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_CONTROL_DIR")
OPEN_INTERVALS = "SELECT * FROM coverage_intervals WHERE ends_at IS NULL"  # What the recorder reads first when it starts


def zero_a_deep_page(path: Path, scan: str) -> int:
    """Turn one 4 KiB page to zeros, as the virtual disk did: a page that ``scan`` reads but the light probe does not. Returns its number."""
    pristine = path.with_name("pristine.sqlite3")
    shutil.copyfile(path, pristine)
    pages = path.stat().st_size // 4096
    for page in range(pages - 1, 1, -1):
        shutil.copyfile(pristine, path)
        with path.open("r+b") as handle:
            handle.seek(page * 4096)
            handle.write(bytes(4096))
        if not db_health.check(full=False, path=path).ok:
            continue
        db = sqlite3.connect(path)
        try:
            db.execute(scan).fetchall()
        except sqlite3.DatabaseError:
            pristine.unlink()
            return page + 1
        finally:
            db.close()
    raise AssertionError("no page was found that the scan reads and the probe does not")


class DamageCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(self.root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.root / "credentials.key")
        os.environ["NVR_CONTROL_DIR"] = str(self.root / "run")
        from app.store import initialize

        initialize()

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def damage_coverage(self) -> None:
        """Thousands of coverage intervals, then one of their pages lost."""
        path = Path(os.environ["NVR_DB_PATH"])
        db = sqlite3.connect(path)  # Foreign keys are off on a plain connection: the rows need no camera
        db.executemany("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,starts_at,checked_at) VALUES('cam','expected_idle',?,?,?)",
                       [("x" * 200, "2026-11-21T03:00:00Z", "2026-11-21T03:39:00Z") for _ in range(4000)])
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        zero_a_deep_page(path, OPEN_INTERVALS)


class DamageReportTests(DamageCase):
    def test_the_first_report_is_kept_and_a_new_start_clears_it(self):
        self.assertIsNone(db_health.damage_reported())
        db_health.report_damage(sqlite3.DatabaseError("database disk image is malformed"), "recorder worker")
        db_health.report_damage(sqlite3.DatabaseError("another"), "ai worker")
        found = db_health.damage_reported()
        self.assertEqual((found["where"], found["error"]), ("recorder worker", "database disk image is malformed"))
        db_health.clear_damage_report()
        self.assertIsNone(db_health.damage_reported())

    def test_reporting_never_raises_when_the_folder_cannot_be_written(self):
        os.environ["NVR_CONTROL_DIR"] = str(self.root / "state.sqlite3" / "not-a-folder")
        with self.assertLogs("app.db_health", level="WARNING"):
            db_health.report_damage(sqlite3.DatabaseError("database disk image is malformed"), "recorder worker")
        self.assertIsNone(db_health.damage_reported())


class HealthCheckTests(DamageCase):
    def test_damage_the_probe_cannot_see_fails_the_health_check_once_the_recorder_has_met_it(self):
        from fastapi.testclient import TestClient

        from app.application import create_app

        self.damage_coverage()
        with TestClient(create_app(start_workers=False), raise_server_exceptions=False) as client:
            self.assertEqual(client.get("/healthz").status_code, 200, "the probe reads only the start of each table: this is the gap")
            environment = {**os.environ, "NVR_SUPERVISED": "1"}
            started = time.monotonic()
            recorder = subprocess.run([sys.executable, "-m", "app.workers.recorder"], env=environment, capture_output=True, text=True, timeout=120)
            self.assertEqual(recorder.returncode, db_health.DAMAGED_EXIT, recorder.stderr)
            self.assertEqual(recorder.stderr.count("THE DATABASE FILE IS DAMAGED"), 1, recorder.stderr)
            self.assertIn(db_health.RECOVERY_DOCUMENT, recorder.stderr)
            self.assertNotIn("Traceback", recorder.stderr)
            self.assertLess(time.monotonic() - started, 60)
            response = client.get("/healthz")
            self.assertEqual((response.status_code, response.json()), (503, {"detail": "Database damaged"}))
            self.assertEqual(db_health.damage_reported()["where"], "recorder worker")

    def test_a_request_that_meets_damage_fails_the_health_check(self):
        from fastapi.routing import APIRoute
        from fastapi.testclient import TestClient

        from app.application import create_app
        from app.store import connect

        self.damage_coverage()
        app = create_app(start_workers=False)

        def scan():
            with connect() as db:
                return len(db.execute(OPEN_INTERVALS).fetchall())

        app.router.routes.insert(0, APIRoute("/api/test-scan", scan, methods=["GET"]))
        with TestClient(app, raise_server_exceptions=False) as client:
            with self.assertLogs("app.application", level="ERROR"):
                self.assertEqual(client.get("/api/test-scan").status_code, 503)
            self.assertEqual(client.get("/healthz").status_code, 503)

    def test_the_nightly_check_fails_the_health_check(self):
        from app import self_test

        self.damage_coverage()
        with self.assertLogs("app.self_test", level="ERROR"):
            self.assertFalse(self_test.run("scheduled")["passed"])
        self.assertEqual(db_health.damage_reported()["where"], "self-test")


class LogFilterTests(DamageCase):
    def setUp(self):
        super().setUp()
        self.filter = db_health.DamageLogFilter("background")
        self.records: list[logging.LogRecord] = []
        self.handler = logging.Handler()
        self.handler.emit = self.records.append
        self.handler.addFilter(self.filter)
        self.logger = logging.getLogger("test.damage-filter")
        self.logger.addHandler(self.handler)
        self.logger.propagate = False

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.propagate = True
        super().tearDown()

    def fail(self, error: Exception) -> None:
        try:
            raise error
        except Exception:  # noqa: BLE001 - what a loop's catch-all does
            self.logger.exception("Camera health sampling failed")

    def test_a_loop_that_meets_damage_every_pass_logs_one_line_then_a_reminder(self):
        for _ in range(50):
            self.fail(sqlite3.DatabaseError("database disk image is malformed"))
        self.assertEqual(len(self.records), 1)
        first = self.records[0]
        self.assertIn("THE DATABASE FILE IS DAMAGED", first.getMessage())
        self.assertIn(db_health.RECOVERY_DOCUMENT, first.getMessage())
        self.assertIsNone(first.exc_info, "no traceback")
        self.assertEqual(db_health.damage_reported()["where"], "background: test.damage-filter")
        self.filter.reminded -= db_health.SUMMARY_SECONDS + 1
        self.fail(sqlite3.DatabaseError("database disk image is malformed"))
        self.fail(sqlite3.DatabaseError("database disk image is malformed"))
        self.assertEqual(len(self.records), 2)
        self.assertIn("still damaged (49 more errors not logged)", self.records[1].getMessage())

    def test_damage_wrapped_in_another_error_is_recognised(self):
        try:
            try:
                raise sqlite3.DatabaseError("database disk image is malformed")
            except sqlite3.DatabaseError as exc:
                raise RuntimeError("Could not plan the backfill") from exc
        except RuntimeError:
            self.logger.exception("Backfill planning failed")
        self.assertIn("THE DATABASE FILE IS DAMAGED", self.records[0].getMessage())

    def test_other_errors_keep_their_traceback_and_report_nothing(self):
        for error in (sqlite3.OperationalError("database is locked"), sqlite3.IntegrityError("UNIQUE constraint failed"), ValueError("bad")):
            self.fail(error)
        self.logger.error("no exception at all")
        self.assertEqual(len(self.records), 4)
        self.assertTrue(all(record.exc_info for record in self.records[:3]))
        self.assertIsNone(db_health.damage_reported())

    def test_a_record_seen_by_two_handlers_is_counted_once(self):
        second: list[logging.LogRecord] = []
        other = logging.Handler()
        other.emit = second.append
        other.addFilter(self.filter)
        self.logger.addHandler(other)
        try:
            for _ in range(3):
                self.fail(sqlite3.DatabaseError("database disk image is malformed"))
        finally:
            self.logger.removeHandler(other)
        self.assertEqual((len(self.records), len(second), self.filter.suppressed), (1, 1, 2))


class PatrolTests(DamageCase):
    """A page of zeros in a hot table is found within minutes, by a check that reads, not by whoever trips over it (#455)."""

    def setUp(self):
        super().setUp()
        from app import self_test

        self_test._patrol = None
        self.addCleanup(setattr, self_test, "_patrol", None)

    def test_a_zeroed_page_of_a_hot_table_raises_the_incident_and_fails_the_health_check(self):
        from fastapi.testclient import TestClient

        from app import self_test
        from app.application import create_app
        from app.store import connect

        self.damage_coverage()
        with TestClient(create_app(start_workers=False), raise_server_exceptions=False) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            with self.assertLogs("app.self_test", level="ERROR") as logged:
                self_test.SelfTestManager.process_once()
            self.assertEqual(len(logged.output), 1, logged.output)
            self.assertIn("coverage_intervals", logged.output[0])
            self.assertIn(db_health.RECOVERY_DOCUMENT, logged.output[0])
            self.assertEqual(client.get("/healthz").status_code, 503)
        self.assertEqual(db_health.damage_reported()["where"], "database patrol")
        with connect() as db:
            incident = db.execute("SELECT summary, fault_active FROM incidents WHERE incident_key='self_test:database'").fetchone()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM self_test_runs").fetchone()[0], 0, "the nightly run was not started on a file known to be damaged")
        self.assertEqual(incident["fault_active"], 1)
        self.assertIn("The database file is damaged", incident["summary"])
        with self.assertNoLogs("app.self_test", level="ERROR"):
            self_test.patrol()  # Known: said once

    def test_a_healthy_file_passes_and_a_pass_is_made_only_every_few_minutes(self):
        from app import self_test

        with patch.object(db_health.Patrol, "run", autospec=True, side_effect=db_health.Patrol.run) as run:
            self.assertTrue(self_test.patrol())
            self.assertTrue(self_test.patrol())
            self.assertEqual(run.call_count, 1)
            self_test._patrol._last -= db_health.PATROL_SECONDS
            self.assertTrue(self_test.patrol())
            self.assertEqual(run.call_count, 2)
        self.assertIsNone(db_health.damage_reported())

    def test_every_pass_checks_the_hot_tables_and_the_others_take_turns(self):
        seen: list[list[str]] = []
        real = db_health.Patrol._table

        def table(db, name):
            seen[-1].append(name)
            return real(db, name)

        db = sqlite3.connect(os.environ["NVR_DB_PATH"])
        others = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} - set(db_health.HOT_TABLES)
        db.close()
        patrol = db_health.Patrol()
        with patch.object(db_health.Patrol, "_table", staticmethod(table)), patch.object(db_health, "PATROL_BUDGET_SECONDS", 0.0):
            for _ in range(len(others)):
                seen.append([])
                self.assertTrue(patrol.run().ok)
        self.assertTrue(all(tuple(names[:2]) == db_health.HOT_TABLES and len(names) == 3 for names in seen), seen[:3])
        self.assertEqual({names[2] for names in seen}, others, "one round covers every table")
        with patch.object(db_health.Patrol, "_table", staticmethod(table)):
            seen.append([])
            patrol.run()
        self.assertEqual(len(seen[-1]), len(others) + 2, "an empty database is gone over whole within the budget")

    def test_a_table_too_large_for_a_pass_is_left_to_the_nightly_check(self):
        self.damage_coverage()
        with patch.object(db_health, "PATROL_TABLE_SECONDS", -1.0), patch.object(db_health, "_PROGRESS_STEPS", 1):
            outcome = db_health.Patrol().run()
        self.assertTrue(outcome.ok, outcome.lines)


class SupervisorTests(DamageCase):
    def exited(self, code: int) -> tuple[float, list[str]]:
        worker = supervisor.Worker("recorder", "app.workers.recorder")
        watched = supervisor.Supervisor([worker])
        with self.assertLogs("nvr.supervisor", level="ERROR") as logged:
            watched._exited(worker, code, 1000.0)
        return worker.next_start_at - 1000.0, logged.output

    def test_a_worker_that_exits_on_damage_is_restarted_after_minutes_not_seconds(self):
        delay, logged = self.exited(db_health.DAMAGED_EXIT)
        self.assertEqual(delay, supervisor.DAMAGED_BACKOFF_SECONDS)
        self.assertIn("the database file is damaged", logged[0])
        self.assertIn(db_health.RECOVERY_DOCUMENT, logged[0])

    def test_any_worker_exit_waits_while_damage_is_on_record(self):
        db_health.report_damage(sqlite3.DatabaseError("database disk image is malformed"), "ai worker")
        delay, _ = self.exited(1)
        self.assertEqual(delay, supervisor.DAMAGED_BACKOFF_SECONDS)

    def test_an_ordinary_exit_keeps_its_short_wait(self):
        delay, _ = self.exited(1)
        self.assertEqual(delay, supervisor.BACKOFF_BASE_SECONDS)

    def test_a_new_start_begins_without_the_old_report(self):
        db_health.report_damage(sqlite3.DatabaseError("database disk image is malformed"), "ai worker")
        with patch.object(supervisor, "Supervisor") as made, patch.object(supervisor.signal, "signal"), patch.object(supervisor.logforward, "install"), \
                patch.object(supervisor.db_health, "install_log_filter"), patch.object(supervisor.logging, "basicConfig"), patch.object(supervisor.os, "umask"):
            made.return_value.run.return_value = 0
            self.assertEqual(supervisor.main(), 0)
        self.assertIsNone(db_health.damage_reported())


if __name__ == "__main__":
    unittest.main()
