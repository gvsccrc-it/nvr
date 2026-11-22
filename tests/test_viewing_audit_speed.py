"""Playing back footage no longer searches the person's whole viewing history on every segment (#461)."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import viewing_audit
from app.store import connect, initialize

TOUCH = ("SELECT id,ended_at FROM viewing_audit WHERE user_id=? AND camera_id=? AND kind='playback' AND ended_at>? ORDER BY id DESC LIMIT 1")
DUE = "SELECT 1 FROM viewing_audit WHERE COALESCE(ended_at,last_seen_at)<? LIMIT 1"


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = mock.patch.dict(os.environ, {"NVR_DB_PATH": str(Path(self.temporary.name) / "state.sqlite3")})
        environment.start()
        self.addCleanup(environment.stop)
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'keeper','x','administrator')")
            for camera in ("c1", "c2"):
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (camera, camera.upper()))

    def history(self, rows: int) -> None:
        """A long viewing history for one account, over a year, as an API token or a video wall builds up."""
        with connect() as db:
            db.execute(f"""
                WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i < {rows - 1})
                INSERT INTO viewing_audit(user_id,username,camera_id,camera_name,kind,started_at,ended_at,last_seen_at)
                SELECT 1,'keeper',CASE i % 2 WHEN 0 THEN 'c1' ELSE 'c2' END,'Camera',CASE i % 3 WHEN 0 THEN 'live' ELSE 'playback' END,
                       strftime('%Y-%m-%dT%H:%M:%fZ','now','-400 days','+' || (i * 60) || ' seconds'),
                       strftime('%Y-%m-%dT%H:%M:%fZ','now','-400 days','+' || (i * 60 + 30) || ' seconds'),
                       strftime('%Y-%m-%dT%H:%M:%fZ','now','-400 days','+' || (i * 60 + 30) || ' seconds')
                FROM n""")


class PlanTests(Case):
    def plan(self, sql: str, values: tuple) -> str:
        with connect() as db:
            return " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, values))

    def test_the_open_session_is_found_through_an_index_on_person_camera_and_end(self):
        self.history(2000)
        plan = self.plan(TOUCH, (1, "c1", viewing_audit.stamp()))
        self.assertIn("viewing_audit_playback_idx (user_id=? AND camera_id=? AND ended_at>?)", plan)

    def test_the_clean_ups_check_for_due_work_does_not_scan_the_table(self):
        self.history(2000)
        plan = self.plan(DUE, (viewing_audit.stamp(),))
        self.assertIn("viewing_audit_finished_idx", plan)
        self.assertNotRegex(plan, r"SCAN viewing_audit$")

    def test_the_code_asks_exactly_these_questions(self):
        # The plans above are only worth something while the code asks the same questions, written the same way
        source = Path(viewing_audit.__file__).read_text(encoding="utf-8")
        self.assertIn('"SELECT id,ended_at FROM viewing_audit WHERE user_id=? AND camera_id=? AND kind=\'playback\' AND ended_at>? "', source)
        self.assertIn('"SELECT 1 FROM viewing_audit WHERE COALESCE(ended_at,last_seen_at)<? LIMIT 1"', source)


class WriteTests(Case):
    def writes(self, touches) -> list[str]:
        seen: list[str] = []
        with connect() as db:
            db.set_trace_callback(seen.append)
            touches(db)
        return [statement for statement in seen if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))]

    def test_a_steady_playback_writes_once_per_camera_in_thirty_seconds(self):
        def sixteen_requests_on_two_cameras(db):
            for _ in range(8):
                viewing_audit.touch_playback(db, 1, "c1")
                viewing_audit.touch_playback(db, 1, "c2")
        written = self.writes(sixteen_requests_on_two_cameras)
        self.assertEqual(len([statement for statement in written if statement.startswith("INSERT")]), 2)  # One session per camera
        self.assertEqual(len(written), 4)  # Each made, and given its start as its end: nothing more for the other fourteen requests
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM viewing_audit WHERE kind='playback'").fetchone()[0], 2)

    def test_once_thirty_seconds_have_passed_the_session_is_extended(self):
        with connect() as db:
            viewing_audit.touch_playback(db, 1, "c1")
            earlier = viewing_audit.stamp(datetime.now(timezone.utc) - timedelta(seconds=45))
            db.execute("UPDATE viewing_audit SET ended_at=?,last_seen_at=?", (earlier, earlier))
        written = self.writes(lambda db: viewing_audit.touch_playback(db, 1, "c1"))
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].startswith("UPDATE"))
        with connect() as db:
            row = db.execute("SELECT COUNT(*),MAX(ended_at) FROM viewing_audit").fetchone()
        self.assertEqual(row[0], 1)
        self.assertGreater(row[1], earlier)


class SpeedTests(Case):
    def test_with_400000_rows_for_one_account_a_segment_requests_audit_takes_under_two_milliseconds(self):
        self.history(400_000)
        with connect() as db:
            viewing_audit.touch_playback(db, 1, "c1")  # The session is open
            began = time.perf_counter()
            for _ in range(200):
                viewing_audit.touch_playback(db, 1, "c1")
            each = (time.perf_counter() - began) / 200
            began = time.perf_counter()
            viewing_audit.prune(db, datetime.now(timezone.utc) - timedelta(days=400))  # Nothing is due
            prune_check = time.perf_counter() - began
        self.assertLess(each, 0.002, f"{each * 1000:.2f} ms a request (46 ms before, the ticket measured)")
        self.assertLess(prune_check, 0.005, f"the clean-up's check took {prune_check * 1000:.1f} ms (27 ms before)")


if __name__ == "__main__":
    unittest.main()
