"""Backfill planning does no work for a camera with nothing new, still plans an outage once it ends, and old jobs go (#475)."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import backfill, coverage
from app.store import connect, initialize

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class PlanningCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = mock.patch.dict(os.environ, {"NVR_DB_PATH": str(Path(self.temporary.name) / "state.sqlite3")})
        environment.start()
        self.addCleanup(environment.stop)
        initialize()
        backfill._last_plan.clear()
        self.addCleanup(backfill._last_plan.clear)

    def camera(self) -> str:
        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,backfill_enabled,created_at) VALUES(?,?,X'01','continuous',1,?)",
                       (camera_id, f"Camera {camera_id}", (NOW - timedelta(days=3)).isoformat()))  # The whole id: names are unique, and 200 six-character prefixes collided about one run in 800
        return camera_id

    def footage(self, camera_id: str, starts: datetime, ends: datetime) -> None:
        """One-minute segments from ``starts`` to ``ends``."""
        with connect() as db:
            moment = starts
            while moment < ends:
                db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,1000)",
                           (camera_id, f"{camera_id}/{uuid.uuid4()}.mp4", moment.isoformat(), (moment + timedelta(minutes=1)).isoformat()))
                moment += timedelta(minutes=1)

    def plan(self, camera_id: str, now: datetime) -> int:
        with connect() as db:
            return backfill.plan_jobs(db, camera_id, now)

    def jobs(self, camera_id: str) -> list[tuple[str, str, str]]:
        with connect() as db:
            return [tuple(row) for row in db.execute("SELECT starts_at,ends_at,state FROM backfill_jobs WHERE camera_id=? ORDER BY starts_at", (camera_id,))]


class NothingNewTests(PlanningCase):
    def test_a_pass_over_200_cameras_with_nothing_new_works_out_no_gaps(self):
        cameras = [self.camera() for _ in range(200)]
        for camera_id in cameras[:5]:  # A few with a day of footage, an outage in it already planned
            self.footage(camera_id, NOW - timedelta(hours=24), NOW - timedelta(hours=10))
            self.footage(camera_id, NOW - timedelta(hours=9), NOW - timedelta(minutes=10))
        with mock.patch.object(coverage, "timeline", wraps=coverage.timeline) as worked_out, connect() as db:
            # One connection for the pass, as the recorder's own pass uses (a connection a camera would time opening connections)
            for camera_id in cameras:
                backfill.plan_jobs(db, camera_id, NOW)
            first = worked_out.call_count
            began = time.perf_counter()
            for camera_id in cameras:
                backfill.plan_jobs(db, camera_id, NOW + timedelta(seconds=30))
            took = time.perf_counter() - began
        self.assertEqual(first, 200)
        self.assertEqual(worked_out.call_count, 200, "a camera with nothing new was worked out again")
        self.assertLess(took, 0.5, f"the second pass took {took * 1000:.0f} ms")
        self.assertEqual(len(self.jobs(cameras[0])), 1)

    def test_new_footage_or_a_change_in_coverage_is_worked_out_again(self):
        camera_id = self.camera()
        self.footage(camera_id, NOW - timedelta(hours=5), NOW - timedelta(hours=1))
        with mock.patch.object(coverage, "timeline", wraps=coverage.timeline) as worked_out:
            self.plan(camera_id, NOW)
            self.plan(camera_id, NOW + timedelta(seconds=30))
            self.assertEqual(worked_out.call_count, 1)
            with connect() as db:
                db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,starts_at) VALUES(?, 'failure', 'camera_offline', ?)",
                           (camera_id, (NOW - timedelta(minutes=30)).isoformat()))
            self.plan(camera_id, NOW + timedelta(seconds=60))
            self.assertEqual(worked_out.call_count, 2)


class OutageEndsTests(PlanningCase):
    def test_an_outage_that_ends_is_planned_within_a_minute_of_settling_though_nothing_changes_meanwhile(self):
        camera_id = self.camera()
        resumed = NOW - timedelta(seconds=40)
        self.footage(camera_id, NOW - timedelta(hours=25), NOW - timedelta(hours=1))  # From before the lookback: no other gap
        self.footage(camera_id, resumed, resumed + timedelta(minutes=1))  # Footage came back 40 s ago and is already indexed
        self.assertEqual(self.plan(camera_id, NOW), 0)  # Inside the 90 s it is left to settle: not planned yet
        self.assertEqual(self.plan(camera_id, NOW + timedelta(seconds=30)), 0)
        planned = self.plan(camera_id, NOW + timedelta(seconds=60))  # Settled now (40 + 60 > 90), with no new data since
        self.assertEqual(planned, 1)
        (job,) = self.jobs(camera_id)
        self.assertEqual((job[0], job[1]), ((NOW - timedelta(hours=1)).isoformat(), resumed.isoformat()))
        self.assertEqual(self.plan(camera_id, NOW + timedelta(seconds=90)), 0)  # And once only


class LookupTests(PlanningCase):
    def test_the_lookup_for_an_existing_job_is_bounded_by_camera_and_time(self):
        with connect() as db:
            plan = " | ".join(row[3] for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM backfill_jobs WHERE camera_id=? AND starts_at<? AND starts_at>? AND ends_at>? LIMIT 1", ("c", "b", "a", "a")))
        self.assertIn("backfill_jobs_camera_idx (camera_id=? AND starts_at>? AND starts_at<?)", plan)
        source = Path(backfill.__file__).read_text(encoding="utf-8")
        self.assertIn('"SELECT 1 FROM backfill_jobs WHERE camera_id=? AND starts_at<? AND starts_at>? AND ends_at>? LIMIT 1"', source)


class PruneTests(PlanningCase):
    def test_jobs_older_than_the_cameras_oldest_footage_are_removed_and_the_rest_kept(self):
        camera_id, other = self.camera(), self.camera()
        self.footage(camera_id, NOW - timedelta(days=2), NOW - timedelta(days=2) + timedelta(minutes=5))
        with connect() as db:
            for cam, starts, state in ((camera_id, NOW - timedelta(days=3), "done"),       # Before the oldest footage: gone
                                       (camera_id, NOW - timedelta(days=3), "failed"),     # Gone too
                                       (camera_id, NOW - timedelta(days=3), "pending"),    # Still to do: kept
                                       (camera_id, NOW - timedelta(days=1), "done"),       # Its footage is still kept: kept
                                       (other, NOW - timedelta(days=30), "done")):          # A camera with no footage at all: kept
                db.execute("INSERT INTO backfill_jobs(id,camera_id,starts_at,ends_at,state,cursor_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), cam, starts.isoformat(), (starts + timedelta(hours=1)).isoformat(), state, starts.isoformat(), NOW.isoformat(), NOW.isoformat()))
            self.assertEqual(backfill.prune_jobs(db), 2)
            self.assertEqual(backfill.prune_jobs(db), 0)
        self.assertEqual([job[2] for job in self.jobs(camera_id)], ["pending", "done"])
        self.assertEqual(len(self.jobs(other)), 1)

    def test_with_nothing_to_remove_it_takes_no_write_lock(self):
        with connect() as db:
            seen: list[str] = []
            db.set_trace_callback(seen.append)
            self.assertEqual(backfill.prune_jobs(db), 0)
        self.assertFalse([statement for statement in seen if statement.lstrip().upper().startswith(("DELETE", "BEGIN"))], seen)

    def test_the_recorders_clean_up_runs_it(self):
        from app.recording import RecordingManager

        with mock.patch.object(backfill, "prune_jobs", return_value=0) as pruned, mock.patch("app.talk.clear_old_recordings"):
            RecordingManager._prune_viewing_audit()
        pruned.assert_called_once()


if __name__ == "__main__":
    unittest.main()
