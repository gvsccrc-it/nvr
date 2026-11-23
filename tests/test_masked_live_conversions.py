"""A masked camera's live re-encode closes soon after its last viewer, and such re-encodes are limited (#488, a follow-up to #344).

#344 made H.265 conversions close 5 seconds after their last viewer and counted them against a limit. A camera whose privacy mask the NVR
draws in is a full decode and encode too, but it kept the ten-minute "keep live video ready" linger and was counted nowhere: someone stepping
through masked cameras, or a wall cycling through them, left one software encode per camera running for ten minutes each. As decided on the
ticket: masked streams close as conversions do, are never held ready, and share one limit with them (6); at the limit idle ones close
first; a masked camera is never shown unmasked to get round it."""
from __future__ import annotations

import asyncio
import json
import types
import unittest
from unittest import mock

from app import live_hub
from app.api import live_media
from app.live_hub import LiveHub, LiveStream, Viewer
from app.viewing import MAX_LIVE_TRANSCODES
from tests.test_live_ready import Case

MASK = "drawbox=x=0:y=0:w=10:h=10:color=black:t=fill"


def join(hub: LiveHub, number: int, masked: bool = True) -> tuple[LiveStream, Viewer]:
    viewer = Viewer()
    stream = hub.join((f"camera-{number}", "sub", "video", "masked" if masked else "direct", "x"), "rtsp://camera.invalid/s", False, viewer,
                      video_filter=MASK if masked else None)
    return stream, viewer


class LingerTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(LiveStream, "start", lambda stream: None)
        patch.start()
        self.addCleanup(patch.stop)

    def linger_after_last_viewer(self, masked: bool) -> float:
        async def scenario():
            hub = LiveHub()
            hub.linger_seconds = 600.0  # "Keep live video ready" is on
            stream, viewer = join(hub, 1, masked)
            stream.linger_seconds = hub.linger_seconds
            hub.leave(stream, viewer)
            return stream.linger.when() - asyncio.get_running_loop().time()

        return asyncio.run(scenario())

    def test_a_masked_stream_closes_soon_after_its_last_viewer_whatever_keep_ready_says(self):
        self.assertLessEqual(self.linger_after_last_viewer(True), live_hub.LINGER_SECONDS + 0.1)

    def test_a_stream_that_is_only_copied_keeps_the_ten_minute_linger(self):
        self.assertGreater(self.linger_after_last_viewer(False), 500)

    def test_masked_streams_are_counted_as_conversions(self):
        async def scenario():
            hub = LiveHub()
            masked, _ = join(hub, 1, True)
            copied, _ = join(hub, 2, False)
            return hub.conversions(), masked

        conversions, masked = asyncio.run(scenario())
        self.assertEqual(conversions, [masked])


class LimitTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(LiveStream, "start", lambda stream: None)
        patch.start()
        self.addCleanup(patch.stop)
        stop = mock.patch.object(LiveStream, "stop", mock.AsyncMock())
        stop.start()
        self.addCleanup(stop.stop)
        live_media._transcodes.clear()
        self.addCleanup(live_media._transcodes.clear)

    def test_opening_and_closing_one_more_masked_camera_than_the_limit_never_passes_it(self):
        async def scenario():
            hub = LiveHub()
            hub.linger_seconds = 600.0
            most = 0
            for number in range(MAX_LIVE_TRANSCODES + 1):
                key = (f"camera-{number}", "sub", "video", "masked", "x")
                self.assertTrue(await live_media._acquire_transcode(key, hub), number)
                stream, viewer = join(hub, number)
                most = max(most, len(hub.conversions()))
                hub.leave(stream, viewer)  # Lingers (5 s) until it is needed
                live_media._release_transcode(key)
            return most, len(hub.conversions())

        most, after = asyncio.run(scenario())
        self.assertLessEqual(most, MAX_LIVE_TRANSCODES)
        self.assertLessEqual(after, MAX_LIVE_TRANSCODES)  # The idle one was closed to make room

    def test_at_the_limit_with_every_conversion_watched_a_new_viewer_is_told_so(self):
        async def scenario():
            hub = LiveHub()
            for number in range(MAX_LIVE_TRANSCODES):
                key = (f"camera-{number}", "sub", "video", "masked", "x")
                await live_media._acquire_transcode(key, hub)
                join(hub, number)  # Watched
            variant = types.SimpleNamespace(transcode=False, video_filter=MASK)
            with mock.patch.object(live_media, "_admit_live_view", return_value=None), mock.patch.object(live_media, "_release_live_view"):
                return await live_media._take_places("camera-new", variant, ("camera-new", "sub", "video", "masked", "x"), hub)

        refusal = asyncio.run(scenario())
        self.assertIsNotNone(refusal)
        self.assertIn("All video conversion slots are in use", json.dumps(refusal.message))

    def test_the_limit_is_six_by_default(self):
        self.assertEqual(MAX_LIVE_TRANSCODES, 6)


class KeepReadyTests(Case):
    def test_keep_ready_never_holds_a_masked_camera(self):
        from app.live_ready import wanted
        from app.store import connect

        self.camera("a", "Alpha")
        self.camera("b", "Bravo")
        with connect() as db:
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,updated_at) VALUES('b',?,'none','2026-01-01T00:00:00Z')",
                       (json.dumps([{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}]),))
            self.assertEqual([key[0] for key, _ in wanted(db)], ["a"])


class StatusTests(unittest.TestCase):
    def test_system_status_counts_masked_re_encodes(self):
        from app.api.operations import _live

        def scenario():
            with mock.patch.object(LiveStream, "start", lambda stream: None):
                hub = LiveHub()
                join(hub, 1, True)
                join(hub, 2, False)
                request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(live_hub=hub)))
                return _live(request)

        async def run():
            return scenario()

        live = asyncio.run(run())
        self.assertEqual((live["conversions"], live["masked_conversions"], live["conversion_limit"]), (1, 1, MAX_LIVE_TRANSCODES))


if __name__ == "__main__":
    unittest.main()
