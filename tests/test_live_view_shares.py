"""One signed-in person, API token or video wall cannot take every live-view place on the server (#487).

Live view was one counter of 16 for the whole server: one person opening a 16-tile grid, or a wall showing 16 cameras, left everyone else
"All live-view slots are in use" on every tile. It counted viewers, although viewers of one camera share one stream. As decided on the
ticket: 16 tiles for each person, token or wall; 32 distinct camera streams for the whole server, watching one already open costing nothing
there; compatibility (MJPEG) tiles 2 each and 4 in all; the refusal says which limit and whose."""
from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

from app import capacity
from app.api import live_media
from app.live_hub import LiveHub
from app.viewing import MAX_LIVE_STREAMS, MAX_LIVE_VIEWERS, MAX_MJPEG, MAX_MJPEG_EACH
from tests.test_support import principal

ALICE, BOB = ("person", "1"), ("person", "2")
COPIED = types.SimpleNamespace(transcode=False, video_filter=None)


def key(camera: str) -> tuple[str, ...]:
    return (camera, "sub", "video", "direct", "x")


class ShareCase(unittest.TestCase):
    def setUp(self):
        self.reset()
        self.addCleanup(self.reset)

    @staticmethod
    def reset():
        with live_media._live_view_lock:
            live_media._live_view_count = 0
            live_media._mjpeg_count = 0
            live_media._places_by_identity.clear()
            live_media._viewers_by_stream.clear()
            live_media._mjpeg_by_identity.clear()
            live_media._live_camera_status.clear()

    def open_tile(self, identity, camera: str):
        """What the WebSocket route does to admit a viewer: a refusal, or None."""
        return asyncio.run(live_media._take_places(camera, COPIED, key(camera), LiveHub(), identity))


class PersonShareTests(ShareCase):
    def test_one_person_opening_sixteen_tiles_does_not_stop_a_second_person(self):
        for number in range(MAX_LIVE_VIEWERS):
            self.assertIsNone(self.open_tile(ALICE, f"camera-{number}"))
        refused = self.open_tile(ALICE, "camera-extra")
        self.assertIsNotNone(refused)
        self.assertIn("you have 16 live tiles open", refused.message["detail"])
        self.assertIsNone(self.open_tile(BOB, "camera-0"))  # Before #487 every tile of the second person was refused
        self.assertIsNone(self.open_tile(BOB, "camera-extra"))

    def test_a_video_wall_and_an_api_token_each_have_their_own_share(self):
        wall = types.SimpleNamespace(wall=True, token_id="wall-token", id=1)
        self.assertEqual(live_media.identity_of(wall), ("wall", "wall-token"))
        token = types.SimpleNamespace(wall=False, token_id="api-token", id=1)
        self.assertEqual(live_media.identity_of(token), ("token", "api-token"))
        self.assertEqual(live_media.identity_of(principal(1, "alice", "operator")), ("person", "1"))
        for number in range(16):
            self.assertIsNone(self.open_tile(("wall", "wall-token"), f"camera-{number}"))
        refused = self.open_tile(("wall", "wall-token"), "camera-extra")
        self.assertIn("this video wall has 16 live tiles open", refused.message["detail"])
        self.assertIsNone(self.open_tile(ALICE, "camera-extra"))  # The wall's own limit affects nobody else

    def test_a_refused_place_and_a_closed_one_are_given_back(self):
        for number in range(MAX_LIVE_VIEWERS):
            self.open_tile(ALICE, f"camera-{number}")
        live_media._release_live_view("camera-0", ALICE, key("camera-0"))
        self.assertIsNone(self.open_tile(ALICE, "camera-again"))
        self.assertEqual(live_media._places_by_identity[ALICE], MAX_LIVE_VIEWERS)


class ServerStreamTests(ShareCase):
    def test_the_server_opens_at_most_32_distinct_streams_but_watching_an_open_one_costs_nothing_there(self):
        people = [("person", str(number)) for number in range(10)]
        opened = 0
        for number in range(MAX_LIVE_STREAMS):
            self.assertIsNone(self.open_tile(people[number % 10], f"camera-{number}"))
            opened += 1
        refused = self.open_tile(("person", "late"), "camera-new")
        self.assertIn("32 camera streams are open", refused.message["detail"])
        self.assertIsNone(self.open_tile(("person", "late"), "camera-5"))  # Already open for someone else: allowed
        self.assertEqual(len(live_media._viewers_by_stream), MAX_LIVE_STREAMS)


class MjpegTests(ShareCase):
    def test_compatibility_tiles_are_two_for_each_person_and_four_in_all(self):
        for _ in range(MAX_MJPEG_EACH):
            self.assertIsNone(live_media._admit_live_view("camera-1", ALICE, mjpeg=True))
        self.assertIn("2 compatibility-mode tiles are open for you", live_media._admit_live_view("camera-1", ALICE, mjpeg=True))
        for _ in range(MAX_MJPEG - MAX_MJPEG_EACH):
            self.assertIsNone(live_media._admit_live_view("camera-1", BOB, mjpeg=True))
        self.assertIn("the server is encoding 4 compatibility-mode tiles", live_media._admit_live_view("camera-1", ("person", "3"), mjpeg=True))
        live_media._release_live_view("camera-1", ALICE, mjpeg=True)
        self.assertIsNone(live_media._admit_live_view("camera-1", ("person", "3"), mjpeg=True))

    def test_the_mjpeg_route_uses_the_persons_share(self):
        import os
        import tempfile
        import uuid
        from pathlib import Path

        from fastapi import HTTPException

        from app.credentials import encrypt
        from app.store import connect, initialize

        with tempfile.TemporaryDirectory() as folder:
            previous = {name: os.environ.get(name) for name in ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH")}
            os.environ["NVR_DB_PATH"], os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(folder) / "db.sqlite3"), str(Path(folder) / "key")
            try:
                initialize()
                camera = str(uuid.uuid4())
                with connect() as db:
                    db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,?)", (camera, "Yard", encrypt("rtsp://camera.invalid/main")))
                alice = principal(1, "alice", "operator")
                with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
                    for _ in range(MAX_MJPEG_EACH):
                        live_media.live_camera(camera, alice, "low", False)
                    with self.assertRaises(HTTPException) as refused:
                        live_media.live_camera(camera, alice, "low", False)
                    self.assertIn("compatibility-mode tiles are open for you", refused.exception.detail)
                    live_media.live_camera(camera, principal(2, "bob", "operator"), "low", False)  # Someone else still can
            finally:
                for name, value in previous.items():
                    os.environ.pop(name, None) if value is None else os.environ.__setitem__(name, value)


class CapacityPlanTests(unittest.TestCase):
    def test_the_capacity_planner_counts_at_most_one_persons_share_of_tiles(self):
        rows = [capacity.Row(name="Many", count=100)]
        sixteen = capacity.estimate(rows, 1, 16, None)["metrics"]["network_out"]["value"]
        sixty_four = capacity.estimate(rows, 1, 64, None)["metrics"]["network_out"]["value"]
        self.assertEqual(sixty_four, sixteen)


if __name__ == "__main__":
    unittest.main()
