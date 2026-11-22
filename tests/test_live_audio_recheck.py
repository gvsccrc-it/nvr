"""Live audio stops when a listener's access ends or the camera's audio is turned off (#489, after #298).

The audio-only stream is played here by a stand-in for FFmpeg that sends a little audio every tenth of a second for ever: the stream
ends only when the NVR ends it."""
from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace
from unittest import mock

from starlette.websockets import WebSocketDisconnect

from app import privacy
from app import principal as principals
from app.api import live_media
from app.store import connect
from tests.test_live_recheck import RecheckCase

FOREVER = "import sys, time\nwhile True:\n    sys.stdout.buffer.write(b'\\xff\\xfb' + b'a' * 400)\n    sys.stdout.buffer.flush()\n    time.sleep(0.1)\n"


class AudioCase(RecheckCase):
    def setUp(self):
        super().setUp()
        with connect() as db:
            db.execute("UPDATE cameras SET audio_supported=1,audio_enabled=1")
        real = asyncio.create_subprocess_exec

        async def stand_in(*args, **kwargs):
            return await real(sys.executable, "-c", FOREVER, stdin=kwargs.get("stdin"), stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"))

        for patched in (mock.patch.object(live_media.shutil, "which", return_value="/usr/bin/ffmpeg"),
                        mock.patch.object(live_media.asyncio, "create_subprocess_exec", stand_in)):
            patched.start()
            self.addCleanup(patched.stop)
        # This application runs no workers: a stand-in for the recorders, which a camera change tells to pick it up
        self.app.state.recorders = SimpleNamespace(sync_camera=mock.AsyncMock())

    def listen(self, principal: principals.Principal, camera: str, change) -> tuple[float, int]:
        """Open the camera's audio-only stream as this principal, make the change once audio is flowing, and return how long the stream
        went on after it and how many pieces arrived after it."""
        response = live_media.live_camera_audio(camera, principal)

        async def run() -> tuple[float, int]:
            pieces = response.body_iterator.__aiter__()
            await asyncio.wait_for(pieces.__anext__(), 10)  # Audio is flowing
            await asyncio.to_thread(change)
            changed, after = time.monotonic(), 0
            try:
                while time.monotonic() - changed < 8:  # Long past the two seconds allowed: a stream that is still going has not been ended
                    await asyncio.wait_for(pieces.__anext__(), 10)
                    after += 1
                await pieces.aclose()
            except StopAsyncIteration:
                pass
            return time.monotonic() - changed, after

        return asyncio.run(run())

    def person(self, client=None, user_id: int | None = None, role: str = "operator") -> principals.Principal:
        return self.session_principal(client or self.limited, self.limited_id if user_id is None else user_id, role)


class AudioOnlyStreamTests(AudioCase):
    def assert_ends_promptly(self, ended: tuple[float, int]) -> None:
        took, _ = ended
        self.assertLess(took, 2.5, f"the audio went on for {took:.1f} s after access ended")

    def test_signing_out_ends_it(self):
        self.assert_ends_promptly(self.listen(self.person(), self.d1, lambda: self.limited.post("/api/auth/logout")))

    def test_losing_the_camera_ends_it(self):
        self.assert_ends_promptly(self.listen(self.person(), self.d1, lambda: self.grant(self.limited_id, [{"scope_type": "group", "group_id": self.yard}])))

    def test_a_disabled_account_ends_it(self):
        def disable():
            answer = self.admin.put(f"/api/admin/users/{self.limited_id}/disabled", json={"disabled": True})
            self.assertEqual(answer.status_code, 200, answer.text)
        self.assert_ends_promptly(self.listen(self.person(), self.d1, disable))

    def test_a_revoked_token_ends_it(self):
        created = self.admin.post("/api/admin/api-tokens", json={"name": "listener", "scopes": ["media", "read"]}).json()
        token = principals.Principal(id=1, username="keeper", role="administrator", scopes=frozenset({"media", "read"}), token_id=created["token"]["id"],
                                     credential=("token", created["token"]["id"]))
        self.assert_ends_promptly(self.listen(token, self.d1, lambda: self.admin.delete(f"/api/admin/api-tokens/{created['token']['id']}")))

    def test_turning_the_cameras_audio_off_ends_it_and_another_cameras_audio_carries_on(self):
        def audio_off():
            answer = self.admin.patch(f"/api/cameras/{self.d1}", json={"audio_enabled": False})
            self.assertEqual(answer.status_code, 200, answer.text)
        self.assert_ends_promptly(self.listen(self.person(), self.d1, audio_off))
        # The other camera: its audio is not touched by that change, and keeps flowing until access ends some other way
        response = live_media.live_camera_audio(self.d2, self.person())

        async def three_seconds() -> int:
            pieces, got = response.body_iterator.__aiter__(), 0
            await asyncio.wait_for(pieces.__anext__(), 10)
            privacy.audio_turned_off(self.d1)  # Again, to be sure it reaches no other camera
            ends = time.monotonic() + 3
            while time.monotonic() < ends:
                await asyncio.wait_for(pieces.__anext__(), 10)
                got += 1
            await pieces.aclose()
            return got

        self.assertGreater(asyncio.run(three_seconds()), 10)

    def test_while_access_stands_it_keeps_flowing(self):
        response = live_media.live_camera_audio(self.d1, self.person())

        async def three_seconds() -> int:
            pieces, got = response.body_iterator.__aiter__(), 0
            ends = time.monotonic() + 3
            while time.monotonic() < ends:
                await asyncio.wait_for(pieces.__anext__(), 10)
                got += 1
            await pieces.aclose()
            return got

        self.assertGreater(asyncio.run(three_seconds()), 15)  # The checks every second do not interrupt it


class LiveSocketAudioTests(AudioCase):
    def joined(self, asked: list[bool]):
        """The live hub's stand-in, noting whether each viewer was joined to a stream that carries the camera's audio."""
        stream = self.StubStream()

        def join(key, source, include_audio, viewer, on_fragment=None, video_filter=None, transcode=False, hevc_copy=False):
            asked.append(include_audio)
            viewer.offer(b"fragment")
            return stream

        return mock.patch.multiple(self.app.state.live_hub, join=join, leave=lambda stream, viewer: None)

    def test_turning_audio_off_ends_a_live_socket_that_carries_it_and_says_why(self):
        asked: list[bool] = []
        with self.joined(asked), self.limited.websocket_connect(f"/api/cameras/{self.d1}/live/stream?audio=true") as websocket:
            self.assertEqual(websocket.receive_json()["type"], "init")
            self.assertEqual(websocket.receive_bytes(), b"init")
            self.assertEqual(websocket.receive_bytes(), b"fragment")
            self.assertEqual(self.admin.patch(f"/api/cameras/{self.d1}", json={"audio_enabled": False}).status_code, 200)
            began = time.monotonic()
            said = websocket.receive_json()
            with self.assertRaises(WebSocketDisconnect) as closed:
                websocket.receive_json()
        self.assertLess(time.monotonic() - began, 2.5)
        self.assertEqual(said["detail"], "This camera's audio was turned off; reconnecting continues without it")
        self.assertEqual(closed.exception.code, live_media.CLOSE_STREAM_FAILED)  # Not a refusal the browser stops at: it reconnects
        # Reconnecting joins a stream of video alone
        with self.joined(asked), self.limited.websocket_connect(f"/api/cameras/{self.d1}/live/stream?audio=true") as websocket:
            self.assertEqual(websocket.receive_json()["type"], "init")
        self.assertEqual(asked, [True, False])

    def test_a_socket_of_video_alone_carries_on_when_another_cameras_audio_is_turned_off(self):
        asked: list[bool] = []
        with self.joined(asked), self.limited.websocket_connect(f"/api/cameras/{self.d2}/live/stream?audio=true") as websocket:
            self.assertEqual(websocket.receive_json()["type"], "init")
            self.assertEqual(websocket.receive_bytes(), b"init")
            self.assertEqual(websocket.receive_bytes(), b"fragment")
            privacy.audio_turned_off(self.d1)
            time.sleep(2.5)  # Two of its checks pass
            websocket.send_json({"type": "listen", "on": False})
            self.assertEqual(websocket.receive_json()["type"], "listening")  # Still open, and answering

    def test_a_stream_without_audio_is_not_ended_by_it(self):
        guard = privacy.StreamGuard(self.person(), self.d1, False)  # Video alone
        audible = privacy.StreamGuard(self.person(), self.d1, False, carries_audio=True)
        privacy.audio_turned_off(self.d1)
        self.assertTrue(guard.still_valid())
        self.assertFalse(audible.still_valid())
        self.assertEqual(audible.reason, "audio")


if __name__ == "__main__":
    import unittest

    unittest.main()
