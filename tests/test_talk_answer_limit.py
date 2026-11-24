"""A camera cannot fill the web process's memory through the talk connection (#469, a follow-up to #378).

The talk reader held whatever body length a camera's RTSP answer declared. An answer now declares a plain number of at most
``talk.MAX_ANSWER_BYTES``, or the connection is closed before its body is read and the talk ends as a camera failure, on record like any other."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app import talk
from app.credentials import encrypt
from app.store import connect
from tests.fixtures.rtsp_backchannel import BackchannelCamera
from tests.test_talk import TalkApiTests


class CountingReader:
    """The camera connection's reader, counting every byte the talk code takes from it."""

    def __init__(self, reader: asyncio.StreamReader, taken: list[int]) -> None:
        self.reader, self.taken = reader, taken

    async def readexactly(self, count: int) -> bytes:
        data = await self.reader.readexactly(count)
        self.taken[0] += len(data)
        return data

    async def readuntil(self, separator: bytes) -> bytes:
        data = await self.reader.readuntil(separator)
        self.taken[0] += len(data)
        return data


def connect_counting(taken: list[int]):
    opened = asyncio.open_connection

    async def open_connection(host, port):
        reader, writer = await opened(host, port)
        return CountingReader(reader, taken), writer
    return open_connection


class AnswerLengthTests(unittest.TestCase):
    def test_a_plain_number_up_to_the_limit_is_taken(self):
        for value, length in ((None, 0), ("", 0), ("0", 0), (" 812 ", 812), (str(talk.MAX_ANSWER_BYTES), talk.MAX_ANSWER_BYTES)):
            with self.subTest(value=value):
                self.assertEqual(talk.answer_length(value), length)

    def test_anything_else_is_refused(self):
        for value in (str(talk.MAX_ANSWER_BYTES + 1), "2000000000", "-5", "+5", "5.0", "lots", "1e9", "٣"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                talk.answer_length(value)

    def test_the_limit_is_well_above_a_session_description(self):
        self.assertGreaterEqual(talk.MAX_ANSWER_BYTES, 16 * 1024)
        self.assertLessEqual(talk.MAX_ANSWER_BYTES, 1024 * 1024)


class HostileCameraTests(unittest.TestCase):
    def attempt(self, camera: BackchannelCamera) -> tuple[Exception, int, talk.RtspBackchannel]:
        taken = [0]

        async def go():
            channel = talk.RtspBackchannel(camera.url)
            with patch.object(talk.asyncio, "open_connection", connect_counting(taken)), self.assertRaises(talk.BackchannelError) as failed:
                await asyncio.wait_for(channel.connect(), 10)
            return failed.exception, channel
        error, channel = asyncio.run(go())
        return error, taken[0], channel

    def test_an_answer_declaring_two_gigabytes_ends_the_talk_and_none_of_it_is_read(self):
        camera = BackchannelCamera(oversized="describe")
        self.addCleanup(camera.close)
        error, taken, channel = self.attempt(camera)
        self.assertIn("2000000000", str(error))
        self.assertNotIsInstance(error, talk.BackchannelUnsupported)  # A camera failure, not a camera without a speaker
        self.assertLessEqual(taken, talk.MAX_ANSWER_BYTES)
        self.assertTrue(channel.closed.is_set())
        # The connection was closed: the camera's sending stopped, far short of what it declared
        self.assertTrue(camera.flood_ended.wait(10))
        self.assertLess(camera.flooded, 64 * 1024 * 1024)

    def test_a_negative_or_non_numeric_length_ends_the_talk_cleanly(self):
        for declared in ("-5", "lots"):
            with self.subTest(declared=declared):
                camera = BackchannelCamera(oversized="describe", declared=declared)
                self.addCleanup(camera.close)
                error, taken, channel = self.attempt(camera)
                self.assertIn("not a number", str(error))
                self.assertLessEqual(taken, talk.MAX_ANSWER_BYTES)
                self.assertTrue(channel.closed.is_set())


class HostileCameraTalkTests(TalkApiTests):
    def test_an_answer_nobody_asked_for_during_a_talk_ends_it_as_a_camera_failure_on_record(self):
        hostile = BackchannelCamera(username="operator", password="S3cret!pass", oversized="after_play")
        self.addCleanup(hostile.close)
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=? WHERE id=?", (encrypt(hostile.url), self.camera_id))
        self.enable()
        with self.client.websocket_connect(self.stream) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "ready")
            ended = websocket.receive_json()
        self.assertEqual((ended["type"], ended["reason"]), ("ended", "camera_failed"))
        self.assertTrue(hostile.flood_ended.wait(10))
        self.assertLess(hostile.flooded, 64 * 1024 * 1024)
        self.assertIsNone(talk.talkers.holder(self.camera_id))
        sessions = self.client.get(f"{self.path}/sessions").json()["sessions"]
        self.assertEqual([session["ended_reason"] for session in sessions], ["camera_failed"])
        self.assertEqual([entry["reason"] for entry in self.audit("talk_ended")], ["camera_failed"])

    def test_a_camera_failing_at_the_start_is_on_record_the_same_way(self):
        hostile = BackchannelCamera(username="operator", password="S3cret!pass", oversized="describe")
        self.addCleanup(hostile.close)
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=? WHERE id=?", (encrypt(hostile.url), self.camera_id))
        self.enable()
        message, code = self.refused(self.client)
        self.assertEqual((message["type"], code), ("error", 4502))
        self.assertIn("at most", message["detail"])
        self.assertEqual([session["ended_reason"] for session in self.client.get(f"{self.path}/sessions").json()["sessions"]], ["camera_failed"])
        self.assertEqual([entry["reason"] for entry in self.audit("talk_ended")], ["camera_failed"])


# Only this module's own tests: the ones borrowed through TalkApiTests run from tests.test_talk
for name in [name for name in vars(TalkApiTests) if name.startswith("test_")]:
    setattr(HostileCameraTalkTests, name, None)
del TalkApiTests

if __name__ == "__main__":
    unittest.main()
