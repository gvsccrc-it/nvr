"""The FFmpeg work a signed-in person can start through playback is bounded (#490).

A snapshot started one FFmpeg per request and frame stepping one ffprobe, with no limit on how many at once; frame stepping held a server
thread for its ffprobe, so forty at once took every thread synchronous routes have. Converted playback queued without bound, re-encoded
H.264 footage that needed nothing, and a queued render went on after its requester had gone. Stand-in processes take the place of FFmpeg."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import httpx
from fastapi import Request

from app import media_gates, privacy
from app.credentials import encrypt
from app.main import app
from app.principal import current_user
from app.store import connect, initialize
from tests.test_support import principal

JPEG = b"\xff\xd8 a picture"


def person_of(request: Request):
    """The signed-in person, as the test's request says (each request of a burst comes from someone)."""
    return principal(int(request.headers.get("x-person", "1")), "person", "operator")


class SlowProcess:
    """FFmpeg or ffprobe taking a while, counted while it runs."""
    running = 0
    most = 0

    def __init__(self, output: bytes) -> None:
        self.output, self.returncode = output, None

    async def communicate(self, *_args):
        SlowProcess.running += 1
        SlowProcess.most = max(SlowProcess.most, SlowProcess.running)
        try:
            await asyncio.sleep(0.4)
        finally:
            SlowProcess.running -= 1
        self.returncode = 0
        return self.output, b""

    def kill(self):
        pass

    async def wait(self):
        return 0


class GateCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        for key, value in (("NVR_DB_PATH", root / "state.sqlite3"), ("NVR_RECORDINGS_PATH", root / "recordings"), ("NVR_CREDENTIAL_KEY_PATH", root / "key")):
            self.addCleanup(lambda key=key, was=os.environ.get(key): os.environ.pop(key, None) if was is None else os.environ.__setitem__(key, was))
            os.environ[key] = str(value)
        initialize()
        self.camera = str(uuid.uuid4())
        media = root / "recordings" / self.camera / "clip.mp4"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"the recording itself")
        self.media = media
        with connect() as db:
            for person in range(1, 60):
                db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(?,?,'unused','operator')", (person, f"person{person}"))
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,video_codec) VALUES(?,?,?,'h264')", (self.camera, "Gate", encrypt("rtsp://camera.invalid/main")))
            self.segment = db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                                      (self.camera, f"{self.camera}/clip.mp4", "2026-10-20T12:00:00+00:00", "2026-10-20T12:01:00+00:00", media.stat().st_size)).lastrowid
        SlowProcess.running = SlowProcess.most = 0
        self.addCleanup(app.dependency_overrides.pop, current_user, None)

    def burst(self, paths: list[tuple[int, str]], ordinary: str) -> tuple[list[int], float]:
        """Send these requests (person, path) all at once, and one ordinary request a moment later: their statuses and how long it took."""
        async def scenario():
            app.dependency_overrides[current_user] = person_of
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://nvr.test") as client:
                burst = [asyncio.create_task(client.get(path, headers={"x-person": str(person)})) for person, path in paths]
                await asyncio.sleep(0.1)
                started = time.monotonic()
                answer = await client.get(ordinary, headers={"x-person": "1"})
                took = time.monotonic() - started
                self.assertEqual(answer.status_code, 200, answer.text)
                responses = await asyncio.gather(*burst)
            return [response.status_code for response in responses], took

        with mock.patch("app.api.playback.shutil.which", return_value="/usr/bin/ffmpeg"), \
                mock.patch("app.api.playback.asyncio.create_subprocess_exec", side_effect=lambda *args, **kwargs: SlowProcess(
                    b'{"frames":[{"best_effort_timestamp_time":"1.0"},{"best_effort_timestamp_time":"2.0"}]}' if "ffprobe" in str(args[0]) or "-show_frames" in args else JPEG)):
            return asyncio.run(scenario())


class BurstTests(GateCase):
    def test_a_burst_of_100_snapshots_runs_a_few_at_a_time_and_ordinary_requests_still_answer(self):
        snapshot = f"/api/cameras/{self.camera}/segments/{self.segment}/snapshot?at=2026-10-20T12:00:30Z"
        statuses, took = self.burst([(1 + number % 50, snapshot) for number in range(100)], "/api/failover/status")
        self.assertLessEqual(SlowProcess.most, media_gates.PROCESSES_IN_ALL)
        self.assertEqual(set(statuses), {200, 429})
        self.assertGreater(statuses.count(429), 50)  # Refused after a short wait, not queued for ever
        self.assertLess(took, 1.0, f"an ordinary request took {took:.2f} s during the burst")

    def test_frame_stepping_holds_no_server_thread_and_is_bounded_too(self):
        step = f"/api/cameras/{self.camera}/segments/{self.segment}/frame-neighbor?at=2026-10-20T12:00:01.500Z&direction=next"
        # 60 at once from 30 people: with a thread each, they took every thread synchronous routes have (40)
        statuses, took = self.burst([(1 + number % 30, step) for number in range(60)], "/api/failover/status")
        self.assertLessEqual(SlowProcess.most, media_gates.PROCESSES_IN_ALL)
        self.assertEqual(set(statuses) - {200, 429}, set())
        self.assertLess(took, 1.0, f"a synchronous route took {took:.2f} s during the burst")

    def test_one_person_has_two_at_a_time(self):
        snapshot = f"/api/cameras/{self.camera}/segments/{self.segment}/snapshot?at=2026-10-20T12:00:30Z"
        statuses, _ = self.burst([(7, snapshot) for _ in range(20)], "/api/failover/status")  # More than two at a time can finish in the 2 s a request waits
        self.assertLessEqual(SlowProcess.most, media_gates.PROCESSES_EACH)
        self.assertIn(429, statuses)


class ConversionTests(GateCase):
    def get(self, path: str, person: int = 1):
        from fastapi.testclient import TestClient

        app.dependency_overrides[current_user] = lambda: principal(person, "person", "operator")
        with TestClient(app) as client:
            return client.get(path)

    def test_compat_on_h264_footage_serves_the_original_file(self):
        with mock.patch.object(privacy, "masked_recording", side_effect=AssertionError("it was converted")):
            response = self.get(f"/api/cameras/{self.camera}/segments/{self.segment}/media?compat=h264")
        self.assertEqual((response.status_code, response.content), (200, b"the recording itself"))

    def test_one_person_may_have_three_conversions_being_prepared(self):
        with connect() as db:
            db.execute("UPDATE cameras SET video_codec='hevc' WHERE id=?", (self.camera,))
        media_gates.CONVERSIONS.by_person[5] = media_gates.CONVERSIONS_EACH  # Three of theirs being prepared or waiting
        try:
            with mock.patch.object(privacy, "masked_recording", side_effect=AssertionError("a fourth was started")):
                refused = self.get(f"/api/cameras/{self.camera}/segments/{self.segment}/media?compat=h264", person=5)
        finally:
            media_gates.CONVERSIONS.by_person.pop(5, None)
        self.assertEqual(refused.status_code, 429)
        self.assertIn("converted recordings being prepared", refused.json()["detail"])
        self.assertEqual(refused.headers["Retry-After"], str(media_gates.RETRY_AFTER_SECONDS))

    def test_a_queued_render_whose_requester_has_gone_does_not_start(self):
        async def gone() -> bool:
            return False

        with mock.patch.object(privacy, "_ffmpeg", side_effect=AssertionError("the render started")):
            with self.assertRaises(privacy.RenderAbandoned):
                asyncio.run(privacy.masked_recording(self.media, [{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}], gone))


if __name__ == "__main__":
    unittest.main()
