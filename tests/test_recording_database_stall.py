"""A database that cannot be written for a while does not stop a healthy recording or lose its last segments (#483).

An error in the two-second watch (a status write waiting longer than the database's time-out) used to leave the watch, end the FFmpeg that
was recording normally and reconnect: every camera together, since they share one database. And a run that ended through an error never
had the rest of its list indexed: its last segments stayed on disk with no row."""
from __future__ import annotations

import asyncio
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from app.recording import RecordingManager
from app.store import connect
import tests.test_run_continuous as pinned  # Not the class by name: the loader would run its tests again here

# An FFmpeg stand-in: a segment and its list line every 0.3 s, and one more as it is asked to stop, as FFmpeg finishes the segment it is in.
RECORDER = '''
import os, signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
open({pids!r}, "a").write(str(os.getpid()) + "\\n")
pattern = Path(sys.argv[-1])
listing = Path(sys.argv[sys.argv.index("-segment_list") + 1])
state = dict(number=0, stop=False)
def segment():
    started = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=10 * state["number"])
    name = started.strftime("segment-%Y%m%dT%H%M%SZ.mp4")
    (pattern.parent / name).write_bytes(b"v" * 500)
    with listing.open("a") as out:
        out.write(f"{{name}},{{10 * state['number']}}.0,{{10 * state['number'] + 10}}.0\\n")
    state["number"] += 1
signal.signal(signal.SIGTERM, lambda *_: state.update(stop=True))
sys.stdin.read()
while not state["stop"]:
    segment()
    time.sleep(0.3)
segment()
'''


class StallCase(unittest.TestCase):
    setUp = pinned.RunContinuousTests.setUp
    tearDown = pinned.RunContinuousTests.tearDown
    camera = pinned.RunContinuousTests.camera
    fake_ffmpeg = pinned.RunContinuousTests.fake_ffmpeg
    run_loop = pinned.RunContinuousTests.run_loop

    def recorder(self) -> Path:
        return self.fake_ffmpeg(RECORDER.format(pids=str(self.root / "pids")))

    def started(self) -> int:
        return len((self.root / "pids").read_text().split())

    def on_disk(self) -> int:
        return len(list((self.root / "recordings").rglob("segment-*.mp4")))

    def indexed(self, camera_id: str) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?", (camera_id,)).fetchone()[0]


class ContinuousTests(StallCase):
    def test_a_stall_of_the_database_does_not_end_ffmpeg_and_its_segments_are_indexed_afterwards(self):
        camera_id = self.camera()
        real = RecordingManager._watch_pass
        calls = {"count": 0}

        def watch_pass(manager, *args):
            calls["count"] += 1
            if calls["count"] <= 2:  # The first two passes, four seconds: nothing can be written
                raise sqlite3.OperationalError("database is locked")
            return real(manager, *args)

        with patch.object(RecordingManager, "_watch_pass", watch_pass), self.assertLogs("nvr.recording", level="WARNING") as logged:
            raised = self.run_loop(camera_id, self.recorder(), seconds=5)
        self.assertIsNone(raised, f"the recording loop died: {raised!r}")
        self.assertGreaterEqual(calls["count"], 3)
        self.assertEqual(self.started(), 1, "the FFmpeg that was recording was ended and another started")
        self.assertGreater(self.on_disk(), 8)
        self.assertEqual(self.indexed(camera_id), self.on_disk(), "everything finished during the stall, and the last segment, has its row")
        self.assertEqual(len([line for line in logged.output if "could not be written" in line]), 1, "said once, not once a pass")

    def test_a_run_that_ends_through_an_error_still_has_the_rest_of_its_list_indexed(self):
        camera_id = self.camera()
        real = RecordingManager._watch_continuous_output
        calls = {"count": 0}

        async def watch(manager, *args):
            calls["count"] += 1
            if calls["count"] == 1:
                await asyncio.sleep(0.8)  # FFmpeg has finished a few segments; nothing has been indexed yet
                raise RuntimeError("something unexpected in the watch")
            await real(manager, *args)

        with patch.object(RecordingManager, "_watch_continuous_output", watch), self.assertLogs("nvr.recording", level="ERROR"):
            raised = self.run_loop(camera_id, self.recorder(), seconds=1.2)
        self.assertIsNone(raised, f"the recording loop died: {raised!r}")
        self.assertGreater(self.on_disk(), 2)
        self.assertEqual(self.indexed(camera_id), self.on_disk())

    def test_a_list_that_cannot_be_indexed_as_its_run_ends_is_kept_and_tried_again(self):
        camera_id = self.camera()
        real = RecordingManager._index_csv
        locked = {"on": True}
        managers: list[RecordingManager] = []

        def index(manager, *args, **options):
            managers.append(manager)
            if locked["on"] and options.get("must_store"):  # As the real one does when its transaction fails on a run's last pass
                raise sqlite3.OperationalError("database is locked")
            return 0 if locked["on"] else real(manager, *args, **options)

        with patch.object(RecordingManager, "_index_csv", index), self.assertLogs("nvr.recording", level="WARNING"):
            raised = self.run_loop(camera_id, self.recorder(), seconds=1)
            manager = managers[0]
            self.assertIsNone(raised, f"the recording loop died: {raised!r}")
            self.assertEqual(self.indexed(camera_id), 0)
            self.assertEqual(len(manager._unindexed), 1)
            manager._index_leftovers()
            self.assertEqual(len(manager._unindexed), 1, "still locked: still kept")
            locked["on"] = False
            manager._index_leftovers()
        self.assertEqual(manager._unindexed, [])
        self.assertGreater(self.on_disk(), 2)
        self.assertEqual(self.indexed(camera_id), self.on_disk())


class MotionTests(StallCase):
    def test_the_motion_buffer_is_left_running_when_its_status_cannot_be_written(self):
        camera_id = self.camera()
        spool = self.root / "spool"
        spool.mkdir()
        (spool / "segment-1.ts").write_bytes(b"x")
        polls = {"count": 0}
        manager = RecordingManager()

        class Assembler:
            async def poll(self):
                polls["count"] += 1
                if polls["count"] == 3:
                    manager.stopping = True

        class Process:
            returncode = None

        def locked(*args):
            raise sqlite3.OperationalError("database is locked")

        async def scenario():
            with patch("app.recording.newest_is_fresh", return_value=True), patch.object(RecordingManager, "_mark_video_fresh", locked), patch("app.recording.asyncio.sleep", side_effect=lambda _: asyncio.sleep(0)):
                await manager._watch_motion_buffer(camera_id, Process(), spool, Assembler(), "rtsp://192.0.2.10/stream")

        with self.assertLogs("nvr.recording", level="WARNING"):
            asyncio.run(scenario())
        self.assertEqual(polls["count"], 3, "the watch went on through the failed writes")


if __name__ == "__main__":
    unittest.main()
