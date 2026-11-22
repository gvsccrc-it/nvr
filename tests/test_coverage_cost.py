"""The coverage record is pruned, and reading it does not read a camera's whole history (#492)."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import coverage
from app.retention import prune_coverage
from app.store import connect, initialize

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
OPEN = "SELECT id,coverage_kind,reason_code FROM coverage_intervals WHERE camera_id=? AND ends_at IS NULL ORDER BY id DESC LIMIT 1"
OLD_WINDOW = ("SELECT coverage_kind,reason,reason_code,starts_at,ends_at FROM coverage_intervals "
              "WHERE camera_id=? AND starts_at<? AND (ends_at IS NULL OR ends_at>?) ORDER BY starts_at LIMIT ?")


class CoverageCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        initialize()
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('cam','Cam',X'01')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('other','Other',X'01')")

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.temporary.cleanup()

    def history(self, days: int, minutes_each: int = 30, camera: str = "cam") -> None:
        """A motion camera's record: idle, covered, idle, covered... up to now, the last interval still open."""
        rows, at = [], NOW - timedelta(days=days)
        index = 0
        while at < NOW:
            ends = at + timedelta(minutes=minutes_each)
            rows.append((camera, "covered" if index % 2 else "expected_idle", "r", None if index % 2 else "no_motion", at.isoformat(), None if ends >= NOW else ends.isoformat(), at.isoformat()))
            at, index = ends, index + 1
        with connect() as db:
            db.executemany("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at,ends_at,checked_at) VALUES(?,?,?,?,?,?,?)", rows)

    def plan(self, sql: str, values: tuple) -> str:
        with connect() as db:
            return " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, values))


class ReadTests(CoverageCase):
    def test_the_open_interval_is_found_through_its_own_index(self):
        plan = self.plan(OPEN, ("cam",))
        self.assertIn("coverage_open_idx", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_the_timeline_reads_only_the_intervals_near_its_window(self):
        plan = self.plan(coverage.COVERAGE_IN_WINDOW, ("cam", "cam", "a", "b", "a", 500))
        self.assertIn("starts_at>? AND starts_at<?", plan)
        self.assertNotIn("TEMP B-TREE", plan)
        self.assertNotIn("SCAN coverage_intervals", plan)

    def test_the_timeline_returns_what_the_plain_overlap_test_returned(self):
        self.history(days=40)
        self.history(days=3, camera="other")
        with connect() as db:
            for start, hours in ((NOW - timedelta(days=1), 24), (NOW - timedelta(days=20, minutes=7), 5), (NOW - timedelta(days=41), 30),
                                 (NOW - timedelta(days=60), 24), (NOW - timedelta(minutes=10), 1), (NOW - timedelta(days=10), 0.2)):
                end = start + timedelta(hours=hours)
                new = [tuple(row) for row in db.execute(coverage.COVERAGE_IN_WINDOW, ("cam", "cam", start.isoformat(), end.isoformat(), start.isoformat(), -1))]
                old = [tuple(row) for row in db.execute(OLD_WINDOW, ("cam", end.isoformat(), start.isoformat(), -1))]
                self.assertEqual(new, old, (start, hours))
            self.assertEqual(len(coverage.timeline(db, "cam", NOW - timedelta(days=1), NOW)["coverage"]), 48)


class PruneTests(CoverageCase):
    def count(self, camera: str = "cam") -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM coverage_intervals WHERE camera_id=?", (camera,)).fetchone()[0]

    def test_intervals_older_than_retention_with_no_footage_left_are_removed(self):
        self.history(days=100)
        before = self.count()
        removed = prune_coverage(NOW)  # The default retention is 30 days
        self.assertEqual(self.count(), before - removed)
        with connect() as db:
            oldest = db.execute("SELECT MIN(ends_at) FROM coverage_intervals WHERE camera_id='cam'").fetchone()[0]
            self.assertEqual(db.execute("SELECT COUNT(*) FROM coverage_intervals WHERE camera_id='cam' AND ends_at IS NULL").fetchone()[0], 1, "the open interval stays")
        self.assertGreaterEqual(oldest, (NOW - timedelta(days=30)).isoformat())
        self.assertLess(oldest, (NOW - timedelta(days=30) + timedelta(hours=1)).isoformat())
        self.assertEqual(prune_coverage(NOW), 0)

    def test_footage_kept_past_its_retention_keeps_its_coverage(self):
        self.history(days=100)
        held_from = NOW - timedelta(days=60)
        with connect() as db:
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES('cam','cam/held.mp4',?,?,1)",
                       (held_from.isoformat(), (held_from + timedelta(minutes=1)).isoformat()))
        prune_coverage(NOW)
        with connect() as db:
            oldest = db.execute("SELECT MIN(ends_at) FROM coverage_intervals WHERE camera_id='cam'").fetchone()[0]
        self.assertLess(oldest, (held_from + timedelta(hours=1)).isoformat())
        self.assertGreaterEqual(oldest, held_from.isoformat())

    def test_the_last_fortnight_is_kept_however_short_the_retention_so_the_weekly_report_is_unchanged(self):
        self.history(days=40)
        with connect() as db:
            report_before = coverage.timeline(db, "cam", NOW - timedelta(days=14), NOW, limit=None)["coverage"]
            self.assertGreater(coverage.prune(db, "cam", 1, NOW), 0)
            report_after = coverage.timeline(db, "cam", NOW - timedelta(days=14), NOW, limit=None)["coverage"]
        self.assertEqual(report_after, report_before)

    def test_a_pass_with_nothing_to_remove_writes_nothing(self):
        import sqlite3

        self.history(days=5)
        holder = sqlite3.connect(os.environ["NVR_DB_PATH"], isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            db = sqlite3.connect(os.environ["NVR_DB_PATH"], timeout=0.2, isolation_level=None)
            db.row_factory = sqlite3.Row
            self.assertEqual(coverage.prune(db, "cam", 30, NOW), 0)
            db.close()
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    def test_another_cameras_record_is_untouched(self):
        self.history(days=100)
        self.history(days=100, camera="other")
        with connect() as db:
            coverage.prune(db, "cam", 30, NOW)
        self.assertGreater(self.count("other"), self.count("cam") * 2)


if __name__ == "__main__":
    unittest.main()
