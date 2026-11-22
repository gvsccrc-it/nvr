"""Footage files with no database row are found and dealt with after the recorder was killed (#484)."""
from __future__ import annotations

import os
import time
import unittest
import uuid
from pathlib import Path

import tests.test_run_continuous as pinned  # Not the class by name: the loader would run its tests again here
from app import prometheus, run_folders
from app.recording import RecordingManager
from app.store import connect

OLD = time.time() - 3600


class RunFolderTests(unittest.TestCase):
    setUp = pinned.RunContinuousTests.setUp
    tearDown = pinned.RunContinuousTests.tearDown
    camera = pinned.RunContinuousTests.camera

    def killed_run(self, camera_id: str, *, finished: int = 3, age: float = 3600, name: str | None = None) -> Path:
        """A run folder as a killed recorder leaves it: ``finished`` segments in FFmpeg's list, none of them indexed, and the one it was writing."""
        from app.storage import camera_recordings_dir

        run = camera_recordings_dir(camera_id) / (name or uuid.uuid4().hex)
        run.mkdir(parents=True)
        when = time.time() - age
        lines = []
        for index in range(finished):
            segment = run / f"segment-20261122T12{index:02d}00Z.mp4"
            segment.write_bytes(b"v" * 500)
            lines.append(f"{segment.name},{index * 60}.0,{index * 60 + 60}.0\n")
        (run / "segments.csv").write_text("".join(lines), encoding="utf-8")
        (run / "segment-20261122T125900Z.mp4").write_bytes(b"u" * 300)  # Being written when FFmpeg died: no list line
        for path in run.iterdir():
            os.utime(path, (when, when))
        return run

    def pass_over(self, manager: RecordingManager, active: set[Path] = frozenset(), after: str = ""):
        with connect() as db:
            cameras = [row[0] for row in db.execute("SELECT id FROM cameras")]
        return run_folders.reconcile(manager, cameras, set(active), after=after)

    def rows(self, camera_id: str) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?", (camera_id,)).fetchone()[0]

    def test_the_next_start_indexes_what_was_finished_and_removes_what_was_not(self):
        camera_id = self.camera()
        run = self.killed_run(camera_id)
        result, following = self.pass_over(RecordingManager())
        self.assertEqual((result.folders, result.indexed, result.removed_files, result.removed_bytes), (1, 3, 1, 300))
        self.assertEqual((result.unindexed_files, result.unindexed_bytes, following), (0, 0, ""))
        self.assertEqual(self.rows(camera_id), 3)
        self.assertEqual(sorted(path.name for path in run.iterdir() if path.suffix == ".mp4"), [f"segment-20261122T12{index:02d}00Z.mp4" for index in range(3)])
        again, _ = self.pass_over(RecordingManager())
        self.assertEqual((again.indexed, again.removed_files, self.rows(camera_id)), (0, 0, 3), "a second pass finds nothing to do")

    def test_a_run_a_recording_is_using_is_never_touched(self):
        camera_id = self.camera()
        run = self.killed_run(camera_id)
        before = sorted(path.name for path in run.iterdir())
        result, _ = self.pass_over(RecordingManager(), active={run})
        self.assertEqual((result.folders, self.rows(camera_id)), (0, 0))
        self.assertEqual(sorted(path.name for path in run.iterdir()), before)

    def test_a_folder_written_to_in_the_last_few_minutes_is_left_alone(self):
        camera_id = self.camera()
        run = self.killed_run(camera_id, age=run_folders.SAFE_AGE_SECONDS - 60)
        result, _ = self.pass_over(RecordingManager())
        self.assertEqual((result.folders, self.rows(camera_id)), (0, 0))
        self.assertTrue((run / "segment-20261122T125900Z.mp4").exists())
        one_fresh = self.killed_run(camera_id)
        os.utime(one_fresh / "segment-20261122T125900Z.mp4")  # Something is still writing this one
        result, _ = self.pass_over(RecordingManager())
        self.assertEqual(result.folders, 0, "one fresh file keeps the whole folder out of the pass")

    def test_nothing_outside_a_cameras_run_folders_is_touched(self):
        from app.storage import camera_recordings_dir

        camera_id = self.camera()
        self.killed_run(camera_id)
        folder = camera_recordings_dir(camera_id)
        motion = folder / "motion"
        motion.mkdir()
        others = [motion / "segment-20261122T120000Z.mp4", folder / "segment-20261122T120000Z.mp4", folder / "notes.txt", folder.parent / "segment-20261122T120000Z.mp4"]
        odd_run = folder / "not-a-run-folder"
        odd_run.mkdir()
        others.append(odd_run / "segment-20261122T125900Z.mp4")
        stranger = folder.parent / "no-such-camera" / uuid.uuid4().hex
        stranger.mkdir(parents=True)
        others.append(stranger / "segment-20261122T125900Z.mp4")
        for path in others:
            path.write_bytes(b"keep")
            os.utime(path, (OLD, OLD))
        self.pass_over(RecordingManager())
        self.assertTrue(all(path.exists() for path in others), [str(path) for path in others if not path.exists()])

    def test_a_file_in_a_run_folder_that_is_not_a_segment_is_left(self):
        camera_id = self.camera()
        run = self.killed_run(camera_id)
        kept = [run / "thumbnail.jpg", run / "segment-broken-name.mp4"]
        for path in kept:
            path.write_bytes(b"keep")
            os.utime(path, (OLD, OLD))
        self.pass_over(RecordingManager())
        self.assertTrue(all(path.exists() for path in kept))

    def test_a_listed_segment_that_cannot_be_indexed_is_counted_not_removed(self):
        camera_id = self.camera()
        run = self.killed_run(camera_id, finished=0)
        tiny = run / "segment-20261122T120000Z.mp4"
        tiny.write_bytes(b"v" * 10)  # Listed, but too small to be video: the indexer drops it
        (run / "segments.csv").write_text(f"{tiny.name},0.0,60.0\n", encoding="utf-8")
        for path in run.iterdir():
            os.utime(path, (OLD, OLD))
        result, _ = self.pass_over(RecordingManager())
        self.assertEqual((result.unindexed_files, result.unindexed_bytes, result.removed_files), (1, 10, 1))
        self.assertTrue(tiny.exists())

    def test_a_run_left_with_nothing_but_its_list_is_removed(self):
        camera_id = self.camera()
        run = self.killed_run(camera_id, finished=0)
        self.pass_over(RecordingManager())
        self.assertFalse(run.exists())

    def test_a_pass_reads_a_bounded_number_of_folders_and_the_next_carries_on(self):
        camera_id = self.camera()
        for index in range(5):
            self.killed_run(camera_id, finished=1, name=f"{index:032x}")
        manager = RecordingManager()
        limit, run_folders.FOLDERS_PER_PASS = run_folders.FOLDERS_PER_PASS, 2
        self.addCleanup(setattr, run_folders, "FOLDERS_PER_PASS", limit)
        seen, after = [], ""
        for _ in range(4):
            result, after = self.pass_over(manager, after=after)
            seen.append(result.folders)
            if not after:
                break
        self.assertEqual(seen, [2, 2, 1])
        self.assertEqual(self.rows(camera_id), 5)

    def test_the_recorders_own_pass_skips_its_running_recordings_and_reports_to_the_metrics(self):
        os.environ["NVR_CONTROL_DIR"] = str(self.root / "run")
        self.addCleanup(os.environ.pop, "NVR_CONTROL_DIR", None)
        camera_id = self.camera()
        old = self.killed_run(camera_id)
        running = self.killed_run(camera_id)
        manager = RecordingManager()
        manager._active_runs.add(running)
        self.assertIsNone(run_folders.last_result())
        with self.assertLogs("nvr.recording", level="INFO") as logged:
            manager._reconcile_run_folders()
        self.assertIn("3 segments indexed, 1 unfinished files removed (300 bytes)", logged.output[0])
        self.assertEqual(self.rows(camera_id), 3)
        self.assertTrue((running / "segment-20261122T125900Z.mp4").exists())
        self.assertFalse((old / "segment-20261122T125900Z.mp4").exists())
        self.assertEqual({key: run_folders.last_result()[key] for key in ("unindexed_files", "unindexed_bytes")}, {"unindexed_files": 0, "unindexed_bytes": 0})
        self.assertRegex(prometheus.render(supervised=False), r"(?m)^nvr_recordings_unindexed_bytes 0$")


if __name__ == "__main__":
    unittest.main()
