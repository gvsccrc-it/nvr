"""The export and redaction queues: a share for each person, a place kept for case packages, people taken in turn, a time limit for a whole
job, and refusals that say how long the wait is (#504).

The export queue held four jobs for the whole installation, with no share per person: four jobs from one operator stopped everyone's exports
and case packages, possibly for hours, and one job could run for days. As decided on the ticket: 8 places, 2 for each person and 4 for an
administrator, one place kept for case packages, people taken in turn, 6 hours for a whole job; redaction reviews 1 for each person, 2 in all,
2 hours each."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import exports, queue_turns, redaction
from app.exports import ExportManager, create_export_record, next_queued
from app.main import app
from app.principal import current_user
from app.store import connect, initialize
from tests.test_support import principal

START = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES", "NVR_EXPORT_QUEUE_PLACES", "NVR_EXPORTS_PER_PERSON",
        "NVR_EXPORTS_PER_ADMINISTRATOR", "NVR_EXPORT_JOB_HOURS")


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        for key in _ENV:
            os.environ.pop(key, None)
        self.root = Path(self.temporary.name) / "recordings"
        self.root.mkdir()
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root)
        initialize()
        self.camera = str(uuid.uuid4())
        (self.root / self.camera).mkdir()
        (self.root / self.camera / "clip.mp4").write_bytes(b"footage")
        with connect() as db:
            for person in range(1, 7):
                db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(?,?,'unused',?)",
                           (person, f"person{person}", "administrator" if person == 6 else "operator"))
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (self.camera, "Queue camera"))
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,7)",
                       (self.camera, f"{self.camera}/clip.mp4", START.isoformat(), (START + timedelta(minutes=1)).isoformat()))

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def ask(self, person: int, case_package: bool = False) -> str:
        return create_export_record(person, [self.camera], START, START + timedelta(seconds=30), ["individual"], case_package=case_package)

    def finished_jobs(self, minutes: float, count: int = 3) -> None:
        """Exports that took ``minutes`` each from start to end, what the estimate of a wait is made from."""
        with connect() as db:
            for _ in range(count):
                started = datetime.now(timezone.utc) - timedelta(days=1)
                db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,output_modes_json,display_settings_json,tile_camera_ids_json,requested_start,"
                           "requested_end,status,created_at,updated_at,started_at,completed_at) VALUES(?,1,'[]','[]','{}','[]',?,?,'completed',?,?,?,?)",
                           (str(uuid.uuid4()), START.isoformat(), START.isoformat(), started.isoformat(), started.isoformat(), started.isoformat(),
                            (started + timedelta(minutes=minutes)).isoformat()))


class ShareTests(QueueCase):
    def test_one_person_with_jobs_queued_cannot_stop_a_second_person_queuing_one(self):
        self.ask(1)
        self.ask(1)
        with self.assertRaises(queue_turns.QueueFull) as refused:
            self.ask(1)
        self.assertIn("You have 2 exports queued or running, the most one person may have at once", str(refused.exception))
        self.ask(2)  # Before #504, four jobs from person 1 refused everyone

    def test_an_administrator_may_have_four(self):
        for _ in range(4):
            self.ask(6)
        with self.assertRaises(queue_turns.QueueFull) as refused:
            self.ask(6)
        self.assertIn("the most an administrator may have", str(refused.exception))

    def test_the_queue_holds_eight_and_keeps_one_place_for_case_packages(self):
        for person, count in ((1, 2), (2, 2), (3, 2), (4, 1)):
            for _ in range(count):
                self.ask(person)
        with self.assertRaises(queue_turns.QueueFull) as refused:
            self.ask(5)  # Seven places taken: the eighth is a case package's
        self.assertIn("The export queue is full: 7 jobs queued or running", str(refused.exception))
        self.ask(5, case_package=True)
        with self.assertRaises(queue_turns.QueueFull):
            self.ask(4, case_package=True)  # Now all eight are taken

    def test_the_limits_come_from_the_environment(self):
        os.environ["NVR_EXPORTS_PER_PERSON"] = "1"
        self.ask(1)
        with self.assertRaises(queue_turns.QueueFull):
            self.ask(1)
        os.environ["NVR_EXPORTS_PER_PERSON"] = "not a number"
        self.assertEqual(queue_turns.export_limits()["each"], 2)


class WaitTests(QueueCase):
    def test_a_refusal_says_how_many_are_ahead_and_roughly_how_long(self):
        self.finished_jobs(minutes=10)
        for person in (2, 3):
            self.ask(person)
        self.ask(1)
        self.ask(1)
        with self.assertRaises(queue_turns.QueueFull) as refused:
            self.ask(1)
        # Person 1's first job has two queued ahead of it: about 10 minutes each, and its own 10
        self.assertEqual(str(refused.exception), "You have 2 exports queued or running, the most one person may have at once. "
                                                 "Your first should finish in about 30 minutes, with 2 jobs ahead of it.")
        self.assertAlmostEqual(refused.exception.retry_after, 1800, delta=5)

    def test_a_full_queue_says_when_the_running_job_should_finish(self):
        self.finished_jobs(minutes=40)
        for person, count in ((1, 2), (2, 2), (3, 2), (4, 1)):
            for _ in range(count):
                self.ask(person)
        with connect() as db:  # The oldest has been running for 15 minutes of its usual 40
            db.execute("UPDATE exports SET status='running',started_at=? WHERE id=(SELECT id FROM exports WHERE status='queued' ORDER BY created_at LIMIT 1)",
                       ((datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(),))
        with self.assertRaises(queue_turns.QueueFull) as refused:
            self.ask(5)
        self.assertIn("A place usually frees in about 25 minutes", str(refused.exception))

    def test_before_any_job_finished_here_a_wait_is_taken_as_five_minutes_a_job(self):
        self.ask(1)
        self.ask(1)
        with self.assertRaises(queue_turns.QueueFull) as refused:
            self.ask(1)
        self.assertIn("Your first should finish in about 5 minutes.", str(refused.exception))  # First in line, nothing running: its own five

    def test_the_api_answers_429_with_retry_after_and_shows_the_limits(self):
        app.dependency_overrides[current_user] = lambda: principal(1, "person1", "operator")
        try:
            with TestClient(app) as client:
                body = {"camera_ids": [self.camera], "start": START.isoformat(), "end": (START + timedelta(seconds=30)).isoformat()}
                for _ in range(2):
                    self.assertEqual(client.post("/api/exports", json=body).status_code, 202)
                refused = client.post("/api/exports", json=body)
                self.assertEqual(refused.status_code, 429)
                self.assertIn("You have 2 exports", refused.json()["detail"])
                self.assertGreater(int(refused.headers["Retry-After"]), 0)
                limits = client.get("/api/exports/limits").json()
        finally:
            app.dependency_overrides.pop(current_user, None)
        self.assertEqual(limits["places"], 8)
        self.assertEqual(limits["each"], 2)
        self.assertEqual(limits["job_hours"], 6)
        self.assertEqual(limits["kept_for_case_packages"], 1)
        self.assertIn(limits["active"], (1, 2))  # The worker may have finished, or failed, one in the meantime


class TurnTests(QueueCase):
    def test_people_are_taken_in_turn_and_one_persons_jobs_oldest_first(self):
        first, second = self.ask(1), self.ask(1)
        other = self.ask(2)
        self.assertEqual(next_queued({}), (first, 1))  # Nobody served yet: the oldest
        self.assertEqual(next_queued({1: 1}), (other, 2))  # Person 1 has had a turn: person 2, though their job is newer
        with connect() as db:
            db.execute("UPDATE exports SET status='completed' WHERE id IN (?,?)", (first, other))
        self.assertEqual(next_queued({1: 1, 2: 2}), (second, 1))

    def test_a_job_whose_run_raised_goes_behind_that_persons_others(self):
        first, second = self.ask(1), self.ask(1)
        self.assertEqual(next_queued({}, {first}), (second, 1))  # It does not hold up the rest...
        with connect() as db:
            db.execute("UPDATE exports SET status='completed' WHERE id=?", (second,))
        self.assertEqual(next_queued({}, {first}), (first, 1))  # ...and is still tried when it is the only one

    def test_the_worker_runs_them_in_that_order(self):
        jobs = [self.ask(1), self.ask(1), self.ask(2)]
        ran: list[str] = []

        async def scenario():
            manager = ExportManager()

            async def execute(job_id):
                ran.append(job_id)
                with connect() as db:
                    db.execute("UPDATE exports SET status='completed' WHERE id=?", (job_id,))

            manager._execute = execute
            manager._worker = asyncio.create_task(manager._run())
            for job_id in jobs:
                manager.enqueue(job_id)
            await manager._queue.join()
            manager._worker.cancel()

        asyncio.run(scenario())
        self.assertEqual(ran, [jobs[0], jobs[2], jobs[1]])


class TimeLimitTests(QueueCase):
    def test_a_job_past_the_whole_job_limit_fails_with_a_clear_reason_and_frees_its_place(self):
        job_id = self.ask(1)
        limits = {**queue_turns.export_limits(), "job_hours": 0.5 / 3600}  # Half a second
        rendering = []

        async def render_forever(manager, job, camera, *args):
            rendering.append(job.id)
            while job.id not in manager._cancelled:  # Stops only when told to, as a render does
                await asyncio.sleep(0.02)
            raise exports.ExportCancelled()

        async def scenario():
            manager = ExportManager()
            await manager._execute(job_id)

        with mock.patch.object(queue_turns, "export_limits", return_value=limits), \
                mock.patch.object(ExportManager, "_render_camera", render_forever):
            started = time.monotonic()
            asyncio.run(scenario())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(rendering, [job_id])
        with connect() as db:
            row = db.execute("SELECT status,error FROM exports WHERE id=?", (job_id,)).fetchone()
            audit = db.execute("SELECT details_json FROM audit_log WHERE action='export_failed' AND resource_id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("the most one export may take, and was stopped", row["error"])
        self.assertIsNotNone(audit)
        self.ask(1)
        self.ask(1)  # Its place is free again

    def test_a_cancel_is_still_a_cancel(self):
        job_id = self.ask(1)

        async def render(manager, job, camera, *args):
            await manager.cancel(job.id)
            raise exports.ExportCancelled()

        with mock.patch.object(ExportManager, "_render_camera", render):
            asyncio.run(ExportManager()._execute(job_id))
        with connect() as db:
            self.assertEqual(db.execute("SELECT status FROM exports WHERE id=?", (job_id,)).fetchone()[0], "cancelled")


class CasePackageTests(QueueCase):
    def test_a_case_package_gets_its_clips_queued_though_others_filled_the_queue(self):
        for person, count in ((1, 2), (2, 2), (3, 2), (4, 1)):
            for _ in range(count):
                self.ask(person)
        calls = []
        real = exports.create_export_record

        def recording(*args, **kwargs):
            calls.append(kwargs.get("case_package"))
            return real(*args, **kwargs)

        from app import cases
        with connect() as db:
            case_id, item_id, package_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            db.execute("INSERT INTO cases(id,title,owner_id,created_at) VALUES(?,?,5,?)", (case_id, "Case", START.isoformat()))
            db.execute("INSERT INTO case_items(id,case_id,kind,camera_id,starts_at,ends_at,added_by,added_at,data_json) VALUES(?,?,'clip',?,?,?,5,?,'{}')",
                       (item_id, case_id, self.camera, START.isoformat(), (START + timedelta(seconds=30)).isoformat(), START.isoformat()))
            db.execute("INSERT INTO case_packages(id,case_id,requested_by,omitted,created_at) VALUES(?,?,5,0,?)", (package_id, case_id, datetime.now(timezone.utc).isoformat()))
            db.execute("INSERT INTO case_package_clips(package_id,item_id) VALUES(?,?)", (package_id, item_id))
        with mock.patch("app.exports.create_export_record", recording):
            cases.advance_packages()
        self.assertEqual(calls, [True])
        with connect() as db:
            self.assertIsNotNone(db.execute("SELECT export_id FROM case_package_clips WHERE package_id=?", (package_id,)).fetchone()[0])


class RedactionQueueTests(QueueCase):
    def review(self, person: int) -> str:
        with mock.patch.object(redaction, "available_kinds", return_value=["faces"]):
            return redaction.create_review(person, [self.camera], START, START + timedelta(seconds=30), ["faces"])

    def test_one_review_for_each_person_and_two_in_all(self):
        self.review(1)
        with self.assertRaises(queue_turns.QueueFull) as mine:
            self.review(1)
        self.assertIn("Your redaction review is still being prepared", str(mine.exception))
        self.review(2)
        with self.assertRaises(queue_turns.QueueFull) as full:
            self.review(3)
        self.assertIn("Redaction reviews are busy: 2 jobs queued or being prepared", str(full.exception))

    def run_review(self, review_id: str, hours: float, slow) -> dict:
        limits = {**queue_turns.review_limits(), "job_hours": hours}
        detector = mock.MagicMock(model_id="stand-in")
        with mock.patch.object(queue_turns, "review_limits", return_value=limits), mock.patch.object(redaction, "fetch_cold_sources", slow):
            asyncio.run(redaction.analyse(review_id, detector))
        with connect() as db:
            return dict(db.execute("SELECT state,error,started_at FROM redaction_reviews WHERE id=?", (review_id,)).fetchone())

    def test_a_review_past_its_limit_fails_with_a_clear_reason_and_frees_its_place(self):
        review_id = self.review(1)
        result = self.run_review(review_id, 0.3 / 3600, lambda *args: time.sleep(1))
        self.assertEqual(result["state"], "failed")
        self.assertIn("the most one may take, and was stopped", result["error"])
        self.assertIsNotNone(result["started_at"])
        self.review(1)  # Its place is free again

    def test_a_timeout_inside_a_review_is_its_own_reason_not_the_limit(self):
        review_id = self.review(1)

        def unanswered(*args):
            raise TimeoutError("The storage tier did not answer")

        result = self.run_review(review_id, 2, unanswered)
        self.assertEqual((result["state"], result["error"]), ("failed", "The storage tier did not answer"))


class WordingTests(unittest.TestCase):
    def test_about(self):
        self.assertEqual([queue_turns.about(seconds) for seconds in (30, 89, 600, 5340, 7200, 21600)],
                         ["about a minute", "about a minute", "about 10 minutes", "about 89 minutes", "about 2 hours", "about 6 hours"])


if __name__ == "__main__":
    unittest.main()
