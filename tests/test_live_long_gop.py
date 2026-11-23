"""A camera whose keyframes are far apart can be joined live by a second viewer, and what is kept for joiners is bounded (#496).

The live stream kept everything since the latest keyframe, without bound, and gave all of it to a viewer who joined. A viewer's queue holds
120 fragments (36 seconds): when the last keyframe was longer ago than that, the joiner was closed as too slow inside ``add`` before it got
anything, and the browser's reconnect met the same wall. With "keep live video ready" on, every real viewer joins late. These tests drive the
stream's own handling of each fragment, with fragments standing in for FFmpeg's."""
from __future__ import annotations

import asyncio
import os
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from app import camera_health, live_hub
from app.store import connect, initialize

FRAGMENT = b"\0" * 1000  # A 300 ms fragment


def stream() -> live_hub.LiveStream:
    return live_hub.LiveStream(("camera-1", "main", "video", "copy", "abc"), "rtsp://camera.invalid/main", False)


def drained(viewer: live_hub.Viewer) -> list[bytes]:
    items = []
    while not viewer.queue.empty():
        items.append(viewer.queue.get_nowait())
    return items


class LongGapTests(unittest.TestCase):
    def run_in_loop(self, work):
        async def scenario():
            return work()

        return asyncio.run(scenario())

    def test_a_late_viewer_of_a_camera_with_a_60_second_keyframe_interval_is_not_closed_as_too_slow(self):
        def work():
            live = stream()
            first = live_hub.Viewer()
            live.add(first)
            live._take(b"key-1", True)
            for number in range(200):  # 60 s of fragments since that keyframe
                live._take(FRAGMENT, False)
                drained(first)  # The first viewer keeps up
            late = live_hub.Viewer()
            live.add(late)
            self.assertIsNone(late.closed_reason)  # Was "too_slow" before it got anything
            self.assertEqual(drained(late), [])  # It is given nothing it could not decode...
            live._take(FRAGMENT, False)
            self.assertEqual(drained(late), [])
            live._take(b"key-2", True)  # ...and starts at the next keyframe
            live._take(b"after-key-2", False)
            self.assertEqual(drained(late), [b"key-2", b"after-key-2"])
            self.assertIsNone(late.closed_reason)
            self.assertEqual(drained(first)[0], FRAGMENT)  # The first viewer was sent every fragment throughout
            return live

        self.run_in_loop(work)

    def test_what_is_kept_for_joiners_is_bounded(self):
        def work():
            live = stream()
            live._take(b"key", True)
            for _ in range(1000):
                live._take(FRAGMENT, False)
                self.assertLessEqual(len(live.gop), live_hub.MAX_GOP_FRAGMENTS)
            self.assertEqual(live.gop, [])  # Past the bound, nothing is kept until the next keyframe
            big = stream()
            big._take(b"key", True)
            for _ in range(10):
                big._take(b"\0" * (8 * 1024 * 1024), False)  # A 4K camera at a high bitrate
                self.assertLessEqual(big._gop_bytes, live_hub.MAX_GOP_BYTES)

        self.run_in_loop(work)

    def test_an_ordinary_camera_still_starts_a_late_viewer_at_the_latest_keyframe_at_once(self):
        def work():
            live = stream()
            live._take(b"key-1", True)
            for _ in range(5):
                live._take(FRAGMENT, False)
            live._take(b"key-2", True)
            live._take(b"after", False)
            late = live_hub.Viewer()
            live.add(late)
            self.assertEqual(drained(late), [b"key-2", b"after"])
            live._take(b"next", False)
            self.assertEqual(drained(late), [b"next"])

        self.run_in_loop(work)

    def test_nothing_is_sent_before_the_first_keyframe(self):
        def work():
            live = stream()
            viewer = live_hub.Viewer()
            live.add(viewer)
            self.assertFalse(live._take(FRAGMENT, False))
            self.assertEqual(drained(viewer), [])
            self.assertTrue(live._take(b"key", True))
            self.assertEqual(drained(viewer), [b"key"])

        self.run_in_loop(work)

    def test_keyframes_too_far_apart_are_reported_and_cleared(self):
        noted: list = []
        clock = {"now": 1000.0}

        async def scenario():
            live = stream()
            # live_hub's own clock only: the event loop keeps the real one (patching time.monotonic itself stopped asyncio.sleep for good)
            stand_in = types.SimpleNamespace(monotonic=lambda: clock["now"])
            with mock.patch.object(live_hub, "time", stand_in), mock.patch.object(camera_health, "note_keyframe_gap", lambda camera, seconds: noted.append((camera, seconds))):
                live._take(b"key", True)
                clock["now"] += 60
                live._take(b"key", True)  # 60 s apart: reported
                clock["now"] += 61
                live._take(b"key", True)  # Much the same: not reported again
                clock["now"] += 2
                live._take(b"key", True)  # Back to normal: cleared
                clock["now"] += 2
                live._take(b"key", True)  # Still normal: nothing to say
                await asyncio.sleep(0.05)

        asyncio.run(scenario())
        self.assertEqual(noted, [("camera-1", 60.0), ("camera-1", None)])


class HealthReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        previous = os.environ.get("NVR_DB_PATH")
        self.addCleanup(lambda: os.environ.pop("NVR_DB_PATH", None) if previous is None else os.environ.__setitem__("NVR_DB_PATH", previous))
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        initialize()
        self.camera = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,X'01')", (self.camera, "Smart codec camera"))

    def test_camera_health_shows_a_camera_whose_keyframes_are_too_far_apart(self):
        camera_health.note_keyframe_gap(self.camera, 60.0)
        with connect() as db:
            report = camera_health.fleet(db, None)
        (row,) = report["cameras"]
        self.assertEqual(row["keyframe_gap_seconds"], 60.0)
        self.assertIn("60.0", camera_health.to_csv(report).splitlines()[1])
        camera_health.note_keyframe_gap(self.camera, None)
        with connect() as db:
            self.assertIsNone(camera_health.fleet(db, None)["cameras"][0]["keyframe_gap_seconds"])
        camera_health.note_keyframe_gap(str(uuid.uuid4()), 30.0)  # A camera deleted meanwhile: nothing is written, nothing raised


if __name__ == "__main__":
    unittest.main()
