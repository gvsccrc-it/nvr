"""The database's write-ahead log shrinks again after a long read (#493), and a worker that is slow to start is not killed as hung (#494)."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import store
from app.store import connect, initialize
from app.workers import liveness, runtime, supervisor

_ENV = ("NVR_DB_PATH", "NVR_CONTROL_DIR", "NVR_DB_LOG_LIMIT_MB", "NVR_DB_SYNCHRONOUS")
MB = 1 << 20


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_CONTROL_DIR"] = str(Path(self.temporary.name) / "run")
        os.environ["NVR_DB_LOG_LIMIT_MB"] = "16"
        initialize()

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()


class LogLimitTests(Case):
    def write(self, times: int) -> None:
        for _ in range(times):
            with connect() as db:
                db.execute("UPDATE filler SET body=? WHERE id=1", (os.urandom(256 * 1024),))

    def test_after_a_long_read_ends_the_log_returns_below_its_limit(self):
        with connect() as db:
            db.execute("CREATE TABLE filler(id INTEGER PRIMARY KEY, body BLOB)")
            db.execute("INSERT INTO filler VALUES(1, x'00')")
        reader = sqlite3.connect(os.environ["NVR_DB_PATH"], isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM filler").fetchone()  # The nightly check, or a backup: a read held open for minutes
        self.write(160)
        held = store.log_bytes()
        self.assertGreater(held, 32 * MB, "the log grows while a read holds it open")
        reader.execute("COMMIT")
        reader.close()
        self.write(40)
        self.assertLessEqual(store.log_bytes(), store.log_limit_bytes(), f"it was {held // MB} MB while the read was open")

    def test_shrinking_empties_the_log_and_says_what_it_was(self):
        other = sqlite3.connect(os.environ["NVR_DB_PATH"])  # Another process has the database open, as one always has: the log file stays
        self.addCleanup(other.close)
        other.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()  # A connection opens the file at its first statement
        with connect() as db:
            db.execute("CREATE TABLE filler(id INTEGER PRIMARY KEY, body BLOB)")
            db.execute("INSERT INTO filler VALUES(1, ?)", (os.urandom(512 * 1024),))
        before, after = store.shrink_log()
        self.assertGreater(before, 0)
        self.assertEqual((after, store.log_bytes()), (0, 0))
        with connect() as db:
            self.assertEqual(db.execute("SELECT length(body) FROM filler").fetchone()[0], 512 * 1024, "nothing was lost")

    def test_shrinking_does_not_wait_for_or_fail_on_a_read_in_progress(self):
        reader = sqlite3.connect(os.environ["NVR_DB_PATH"], isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        with connect() as db:
            db.execute("CREATE TABLE filler(id INTEGER PRIMARY KEY, body BLOB)")
        try:
            started = time.monotonic()
            store.shrink_log()
            self.assertLess(time.monotonic() - started, 5)
        finally:
            reader.close()

    def test_the_limit_is_a_setting_kept_within_bounds_and_synchronous_is_untouched(self):
        for given, megabytes in (("64", 64), ("1", 16), ("999999", 4096), ("many", 128)):
            os.environ["NVR_DB_LOG_LIMIT_MB"] = given
            self.assertEqual(store.log_limit_bytes(), megabytes * MB, given)
        os.environ.pop("NVR_DB_LOG_LIMIT_MB")
        self.assertEqual(store.log_limit_bytes(), 128 * MB)
        with connect() as db:
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 1, "NORMAL, the owner's choice")
            self.assertEqual(db.execute("PRAGMA journal_size_limit").fetchone()[0], 128 * MB)

    def test_the_logs_size_is_in_the_metrics(self):
        from app import prometheus

        text = prometheus.render(supervised=False)
        for name in ("nvr_database_bytes", "nvr_database_log_bytes", "nvr_database_log_limit_bytes"):
            self.assertRegex(text, rf"(?m)^{name} \d+")

    def test_the_nightly_self_test_logs_the_size_before_and_after(self):
        from app import self_test

        self_test._patrol = None
        with patch.object(self_test, "due", return_value=True), patch.object(self_test, "run") as run, patch("app.coverage.publish_weekly_report"), \
                self.assertLogs("app.self_test", level="INFO") as logged:
            self_test.SelfTestManager.process_once()
        run.assert_called_once_with("scheduled")
        self.assertTrue(any("write-ahead log was" in line and "MB now" in line for line in logged.output), logged.output)


class SlowStartTests(Case):
    def worker(self, age: float) -> supervisor.Worker:
        worker = supervisor.Worker("recorder", "app.workers.recorder")
        worker.process = type("Process", (), {"pid": os.getpid(), "poll": lambda self: None})()
        worker.started_at = time.monotonic() - age
        return worker

    def hung(self, age: float) -> bool:
        return supervisor.Supervisor([])._hung(self.worker(age), time.monotonic())

    def test_a_worker_still_starting_after_45_seconds_is_not_taken_for_hung(self):
        liveness.mark_starting("recorder")
        self.assertFalse(self.hung(45))
        self.assertFalse(self.hung(liveness.START_LIMIT_SECONDS - 1))

    def test_a_worker_that_never_finishes_starting_is_ended_at_the_limit_with_that_reason(self):
        liveness.mark_starting("recorder")
        self.assertTrue(self.hung(liveness.START_LIMIT_SECONDS + 1))
        worker = self.worker(liveness.START_LIMIT_SECONDS + 1)
        killed = []
        worker.process.kill = lambda: killed.append(True)
        worker.process.wait = lambda: -9
        watched = supervisor.Supervisor([worker])
        with patch.object(supervisor, "end_group", return_value=False), patch.object(watched, "_exited") as exited, self.assertLogs("nvr.supervisor", level="WARNING"):
            watched._watch(worker)
        self.assertEqual(killed, [True])
        self.assertIn("had not finished starting after 300 s", worker.killed_reason)
        self.assertNotIn("event loop was blocked", worker.killed_reason)
        exited.assert_called_once()

    def test_a_running_worker_that_goes_silent_is_still_hung_after_30_seconds(self):
        self.assertTrue(self.hung(45), "no start-up file: the usual rule")
        liveness.mark_starting("recorder")
        liveness.mark_started("recorder")
        self.assertTrue(self.hung(45), "it had started: the usual rule")
        liveness.pulse("recorder")
        self.assertFalse(self.hung(45))

    def test_another_process_s_start_up_file_does_not_count(self):
        liveness._starting_path("recorder").parent.mkdir(parents=True, exist_ok=True)
        liveness._starting_path("recorder").write_text(str(os.getpid() + 1), encoding="ascii")
        self.assertTrue(self.hung(45))

    def test_a_worker_says_it_is_starting_until_its_start_up_has_finished_and_logs_how_long_it_took(self):
        seen = {}

        class Slow:
            async def start(self):
                seen["during"] = (liveness.starting("recorder", os.getpid()), liveness.pulse_fresh("recorder", os.getpid()))
                await asyncio.sleep(0.2)

            async def stop(self):
                seen["stopped"] = True

            async def handle(self, message):
                return None

        async def scenario():
            task = asyncio.create_task(runtime.run("recorder", Slow()))
            await asyncio.sleep(0.5)
            seen["after"] = (liveness.starting("recorder", os.getpid()), liveness.pulse_fresh("recorder", os.getpid()))
            os.kill(os.getpid(), 15)  # What the supervisor sends to stop a worker
            await asyncio.wait_for(task, timeout=10)

        with patch.object(runtime.os, "umask"), self.assertLogs("nvr.recorder", level=logging.INFO) as logged:
            asyncio.run(scenario())
        self.assertEqual(seen["during"], (True, False), "starting, and no pulse yet")
        self.assertEqual(seen["after"], (False, True), "started, and pulsing")
        self.assertTrue(seen.get("stopped"))
        self.assertRegex(logged.output[0], r"The recorder worker started in 0\.\d s")


if __name__ == "__main__":
    unittest.main()
