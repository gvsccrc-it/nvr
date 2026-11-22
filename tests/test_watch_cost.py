"""The recorder's two-second watch costs the same for a camera connected for a month as for a minute (#481)."""
from __future__ import annotations

import os
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import tests.test_run_continuous as pinned  # Not the class by name: the loader would run its tests again here
from app import recording
from app.recording import RecordingManager
from app.store import connect

SEGMENTS = 50_000


class WatchCostTests(unittest.TestCase):
    setUp = pinned.RunContinuousTests.setUp
    tearDown = pinned.RunContinuousTests.tearDown
    camera = pinned.RunContinuousTests.camera

    def long_run(self, camera_id: str, count: int) -> tuple[Path, Path]:
        """A run folder as it is after a month without a reconnect: a file and a list line for every segment, all of them indexed."""
        folder = self.root / "recordings" / camera_id / "run"
        folder.mkdir(parents=True)
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        names = [(start + timedelta(seconds=60 * index)).strftime("segment-%Y%m%dT%H%M%SZ.mp4") for index in range(count)]
        for name in names:
            with open(folder / name, "wb") as out:
                out.write(b"v" * 200)
        listing = folder / "segments.csv"
        listing.write_text("".join(f"{name},{index * 60}.0,{index * 60 + 60}.0\n" for index, name in enumerate(names)), encoding="utf-8")
        return folder, listing

    def test_a_pass_over_a_run_of_fifty_thousand_segments_with_nothing_new_takes_milliseconds(self):
        camera_id = self.camera()
        folder, listing = self.long_run(camera_id, SEGMENTS)
        manager = RecordingManager()
        self.assertEqual(manager._index_csv(camera_id, listing, folder), SEGMENTS)
        os.utime(sorted(folder.glob("segment-*.mp4"))[-1])  # The newest is being written now
        manager._watch_pass(camera_id, listing, folder, 60)  # The first pass finds the newest file
        passes = []
        for _ in range(20):
            started = time.perf_counter()
            manager._watch_pass(camera_id, listing, folder, 60)
            passes.append(time.perf_counter() - started)
        self.assertLess(min(passes) * 1000, 5.0, f"the quickest of 20 passes: {min(passes) * 1000:.1f} ms")
        self.assertLess(sorted(passes)[len(passes) // 2] * 1000, 25.0, "and the usual one is no worse than a few times that on a busy machine")

    def test_new_lines_are_indexed_from_where_the_last_pass_stopped(self):
        camera_id = self.camera()
        folder, listing = self.long_run(camera_id, 50)
        manager = RecordingManager()
        self.assertEqual(manager._index_csv(camera_id, listing, folder), 50)
        (folder / "segment-20261101T000000Z.mp4").write_bytes(b"v" * 200)
        with listing.open("a", encoding="utf-8") as out:
            out.write("segment-20261101T000000Z.mp4,3000.0,3060.0\n")
            out.write("segment-20261101T000100Z.mp4,3060.0")  # Being written: no line end yet
        self.assertEqual(manager._index_csv(camera_id, listing, folder), 1)
        offset, last = manager._indexed_lines[listing]
        self.assertEqual(last, "segment-20261101T000000Z.mp4,3000.0,3060.0\n")
        self.assertEqual(offset, listing.stat().st_size - len("segment-20261101T000100Z.mp4,3060.0"))
        (folder / "segment-20261101T000100Z.mp4").write_bytes(b"v" * 200)
        with listing.open("a", encoding="utf-8") as out:
            out.write(",3120.0\n")
        self.assertEqual(manager._index_csv(camera_id, listing, folder), 1, "the line that was unfinished is read again, whole")
        self.assertEqual(manager._index_csv(camera_id, listing, folder), 0)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?", (camera_id,)).fetchone()[0], 52)

    def test_a_list_that_was_replaced_is_read_from_its_start(self):
        camera_id = self.camera()
        folder, listing = self.long_run(camera_id, 5)
        manager = RecordingManager()
        self.assertEqual(manager._index_csv(camera_id, listing, folder), 5)
        (folder / "segment-20270101T000000Z.mp4").write_bytes(b"v" * 200)
        listing.write_text("segment-20270101T000000Z.mp4,0.0,60.0\n" * 9, encoding="utf-8")  # Longer than what was remembered, and not the same list
        self.assertEqual(manager._index_csv(camera_id, listing, folder), 1)

    def test_the_newest_segment_is_read_through_the_index_without_a_scan_or_a_sort(self):
        with connect() as db:
            plan = " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT ends_at FROM recording_segments WHERE camera_id=? ORDER BY starts_at DESC LIMIT 1", ("x",)))
        self.assertIn("SEARCH", plan)
        self.assertNotIn("SCAN", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_ten_minutes_of_steady_recording_write_the_cameras_row_less_than_once_in_five_seconds(self):
        camera_id = self.camera()
        folder, listing = self.long_run(camera_id, 3)
        manager = RecordingManager()
        manager._index_csv(camera_id, listing, folder)
        newest = sorted(folder.glob("segment-*.mp4"))[-1]
        with connect() as db:
            db.execute("UPDATE recording_segments SET ends_at=? WHERE camera_id=?", ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), camera_id))
            db.execute("CREATE TABLE camera_writes(at TEXT)")
            db.execute("CREATE TRIGGER count_camera_writes AFTER UPDATE ON cameras BEGIN INSERT INTO camera_writes VALUES('x'); END")
        clock = {"now": 1000.0}
        fake = types.SimpleNamespace(monotonic=lambda: clock["now"], time=lambda: clock["now"], perf_counter=time.perf_counter, sleep=time.sleep)
        with patch.object(recording, "time", fake):
            for _ in range(300):  # A pass every two seconds for ten minutes
                os.utime(newest, (clock["now"], clock["now"]))  # FFmpeg is writing it
                manager._watch_pass(camera_id, listing, folder, 60)
                clock["now"] += 2.0
        with connect() as db:
            writes = db.execute("SELECT COUNT(*) FROM camera_writes").fetchone()[0]
            row = db.execute("SELECT recording_status,last_video_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
        self.assertLess(writes, 600 / 5 + 12, "it was 600: two a pass")
        self.assertGreater(writes, 600 / 5 / 2, "but the row is still kept current")
        self.assertEqual(row["recording_status"], "recording")
        self.assertIsNotNone(row["last_video_at"])

    def test_a_segment_that_goes_quiet_is_noticed_and_the_next_one_is_found(self):
        camera_id = self.camera()
        folder, listing = self.long_run(camera_id, 3)
        manager = RecordingManager()
        first, second, third = sorted(folder.glob("segment-*.mp4"))
        old = time.time() - 60
        for path in (first, second, third):
            os.utime(path, (old, old))
        self.assertFalse(manager._being_written(folder), "nothing has been written for a minute")
        os.utime(third)
        with patch.object(recording, "RELIST_SECONDS", 0.0):
            self.assertTrue(manager._being_written(folder))
            os.utime(third, (old, old))
            later = folder / "segment-20261201T000000Z.mp4"
            later.write_bytes(b"v")
            self.assertTrue(manager._being_written(folder), "FFmpeg moved on to a new file: it is found")
            later.unlink()
            self.assertFalse(manager._being_written(folder), "a file removed under the watch is not an error")


if __name__ == "__main__":
    unittest.main()
