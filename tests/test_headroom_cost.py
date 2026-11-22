"""Keeping free space reads the oldest footage through the index, a page at a time, never the whole table (#480)."""
from __future__ import annotations

import itertools
import tracemalloc
from datetime import timedelta

from app import headroom
from app.store import connect
from tests.test_headroom import NOW, UNIT, HeadroomCase

ROWS = 60_000


class CostTests(HeadroomCase):
    CAPACITY = ROWS * UNIT * 2

    def many(self, count: int) -> None:
        """A month of footage on the default volume for two cameras, as rows only (the selection reads no files)."""
        from app.storage import storage_status

        storage_status()  # A first use: the volume gets its marker before it holds any footage (#479)
        rows = []
        for index in range(count):
            camera = self.cameras["north" if index % 2 else "south"]
            ends = NOW - timedelta(minutes=count - index)
            rows.append((camera, f"{camera}/segment-{index:07d}.mp4", (ends - timedelta(minutes=1)).isoformat(), ends.isoformat(), UNIT))
        with connect() as db:
            db.executemany("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)", rows)

    def test_the_selection_uses_the_camera_index_with_no_scan_of_the_table_and_no_sort(self):
        from app.volume_marker import _prefix_filter

        where, values = _prefix_filter("default", {"default": "", "archive": "archive"}, available_only=False)
        with connect() as db:
            plan = [row[3] for row in db.execute(
                f"EXPLAIN QUERY PLAN SELECT id,relative_path,starts_at,ends_at FROM recording_segments s WHERE camera_id=? AND starts_at>? AND {where} "
                "AND NOT EXISTS (SELECT 1 FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>?)) "
                "AND NOT EXISTS (SELECT 1 FROM export_sources x JOIN exports e ON e.id=x.export_id WHERE x.segment_id=s.id AND e.status IN ('queued','running')) "
                "ORDER BY starts_at LIMIT 100", ("camera", "", *values, NOW.isoformat()))]
        self.assertTrue(plan[0].startswith("SEARCH s USING INDEX"), plan)
        self.assertIn("camera_id=? AND starts_at>?", plan[0])
        self.assertFalse(any("SCAN s" in line or "TEMP B-TREE" in line for line in plan), plan)

    def test_taking_two_hundred_of_sixty_thousand_reads_a_few_hundred_rows_and_little_memory(self):
        self.many(ROWS)
        fetched = {"rows": 0}
        real = headroom._oldest_of_camera

        def counted(*args):
            for row in real(*args):
                fetched["rows"] += 1
                yield row

        headroom._oldest_of_camera, restore = counted, real
        self.addCleanup(setattr, headroom, "_oldest_of_camera", restore)
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        taken = list(itertools.islice(headroom.oldest_removable("default", {"default": "", "archive": "archive"}, NOW), 200))
        peak = tracemalloc.get_traced_memory()[1] - before
        tracemalloc.stop()
        self.assertEqual(len(taken), 200)
        self.assertEqual([row["ends_at"] for row in taken], sorted(row["ends_at"] for row in taken), "oldest first, across the cameras")
        self.assertEqual(taken[0]["relative_path"].rsplit("-", 1)[1], "0000000.mp4")
        self.assertLessEqual(fetched["rows"], 200 + len(self.cameras), "one row ahead for each camera, no more")
        self.assertLess(peak, 2 * 1024 * 1024, f"{peak} bytes")

    def test_a_pass_removes_the_oldest_of_a_large_volume_and_stops_when_the_headroom_is_free(self):
        self.CAPACITY = 2000 * UNIT  # The disk is exactly full
        self.many(2000)
        headroom.set_headroom("default", 100 * UNIT)  # 100 segments have to go
        result = headroom.keep_headroom(NOW)["default"]
        self.assertEqual(result["removed_segments"], 100)
        with connect() as db:
            oldest = db.execute("SELECT MIN(relative_path) FROM recording_segments").fetchone()[0]
        self.assertTrue(oldest.endswith("segment-0000100.mp4"), oldest)

    def test_a_long_run_of_held_footage_at_the_front_does_not_use_up_the_pass(self):
        self.many(1000)
        with connect() as db:
            db.execute("INSERT INTO recording_holds(id,segment_id,camera_id) SELECT 'hold-'||id,id,camera_id FROM recording_segments ORDER BY id LIMIT 600")
        taken = list(itertools.islice(headroom.oldest_removable("default", {"default": "", "archive": "archive"}, NOW), 5))
        self.assertEqual([row["relative_path"].rsplit("-", 1)[1] for row in taken], [f"{index:07d}.mp4" for index in range(600, 605)])
