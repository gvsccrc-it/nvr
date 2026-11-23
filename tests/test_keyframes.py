"""Plate and face reading take full-size frames from one keyframe decoder per camera, not a new FFmpeg and camera session per attempt (#518).

The stand-in decoder below writes numbered BMP pictures the way FFmpeg does, and counts how often it is started: that count is the number of
camera sessions the reading opens.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app import keyframes
from app.keyframes import KeyframeSource
from tests.test_plate_full_size import bmp

SHAPE = (36, 64, 3)  # Small, so the tests move little data; the real decoder's size is tested against FFmpeg below


class StandInDecoder:
    """A process whose output is a numbered BMP every ``every`` seconds, until it is ended or has written ``frames`` of them."""

    def __init__(self, every: float = 0.02, frames: int | None = None, first: bytes = b"") -> None:
        self.stdout = asyncio.StreamReader()
        self.returncode: int | None = None
        self.number = 0
        self._writer = asyncio.create_task(self._write(every, frames, first))

    async def _write(self, every: float, frames: int | None, first: bytes) -> None:
        if first:
            self.stdout.feed_data(first)
        while frames is None or self.number < frames:
            await asyncio.sleep(every)
            if self.returncode is not None:
                return
            self.number += 1
            self.stdout.feed_data(bmp(np.full(SHAPE, self.number % 256, dtype=np.uint8)))
        self.stdout.feed_eof()

    def terminate(self) -> None:
        self._end()

    def kill(self) -> None:
        self._end()

    def _end(self) -> None:
        if self.returncode is None:
            self.returncode = -15
            self._writer.cancel()
            self.stdout.feed_eof()

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


class Starts:
    """A stand-in ``start``: each call is one camera session."""

    def __init__(self, **decoder) -> None:
        self.decoder, self.processes, self.addresses = decoder, [], []

    async def __call__(self, address: str) -> StandInDecoder:
        self.addresses.append(address)
        process = StandInDecoder(**self.decoder)
        self.processes.append(process)
        return process


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, 30))


class SteadyTrafficTests(unittest.TestCase):
    def test_a_camera_read_every_moment_keeps_one_session_and_one_process(self):
        async def scenario():
            starts = Starts()
            source = KeyframeSource(lambda: "rtsp://camera.example.test/main", start=starts)
            frames = []
            for _ in range(25):  # Reads as fast as the attempts can come; the real attempts are 3 s apart and the decoder stays open 30 s
                frames.append(await source.frame(timeout=2))
                await asyncio.sleep(0.01)
            open_before_close = source.open
            await source.close()
            return starts, frames, open_before_close

        starts, frames, open_before_close = run(scenario())
        self.assertEqual(len(starts.processes), 1)
        self.assertTrue(open_before_close)
        self.assertTrue(all(frame is not None and frame.shape == SHAPE for frame in frames))
        self.assertEqual(starts.processes[0].returncode, -15)  # Ended when the camera's analysis ended

    def test_plate_and_face_reading_asking_at_once_share_the_decoder_and_the_frame(self):
        async def scenario():
            starts = Starts()
            source = KeyframeSource(lambda: "rtsp://camera.example.test/main", start=starts)
            plate, face = await asyncio.gather(source.frame(timeout=2), source.frame(timeout=2))
            await source.close()
            return starts, plate, face

        starts, plate, face = run(scenario())
        self.assertEqual(len(starts.processes), 1)
        self.assertIs(plate, face)

    def test_a_frame_is_the_next_keyframe_after_the_request_never_an_older_one(self):
        async def scenario():
            starts = Starts(every=0.05)
            source = KeyframeSource(lambda: "rtsp://camera.example.test/main", start=starts)
            await source.frame(timeout=2)
            await asyncio.sleep(0.2)  # Several keyframes arrive that nobody asked for
            written = starts.processes[0].number
            frame = await source.frame(timeout=2)
            await source.close()
            return written, frame

        written, frame = run(scenario())
        self.assertGreater(int(frame[0, 0, 0]), written % 256)

    def test_nothing_is_converted_for_keyframes_nobody_is_waiting_for(self):
        async def scenario():
            starts = Starts(every=0.01)
            source = KeyframeSource(lambda: "rtsp://camera.example.test/main", start=starts)
            with patch("app.plates.decode_bmp", wraps=keyframes_decode()) as decode:
                await source.frame(timeout=2)
                await asyncio.sleep(0.3)
                written = starts.processes[0].number
                await source.close()
            return written, decode.call_count

        written, decoded = run(scenario())
        self.assertGreater(written, 10)
        self.assertEqual(decoded, 1)


def keyframes_decode():
    from app.plates import decode_bmp
    return decode_bmp


class BoundedStartsTests(unittest.TestCase):
    def test_a_quiet_camera_holds_no_session_and_a_later_vehicle_opens_one_again(self):
        async def scenario():
            starts = Starts()
            source = KeyframeSource(lambda: "rtsp://camera.example.test/main", start=starts, idle=0.2, restart=0.0)
            await source.frame(timeout=2)
            await asyncio.sleep(0.6)  # Nobody asks for longer than the idle time
            closed_when_quiet = not source.open and starts.processes[0].returncode is not None
            frame = await source.frame(timeout=2)
            await source.close()
            return starts, closed_when_quiet, frame

        starts, closed_when_quiet, frame = run(scenario())
        self.assertTrue(closed_when_quiet)
        self.assertIsNotNone(frame)
        self.assertEqual(len(starts.processes), 2)

    def test_a_decoder_that_keeps_failing_is_started_at_most_six_times_a_minute(self):
        # A camera that hangs up at once, asked for a frame every second for ten minutes by the clock the source reads.
        async def scenario():
            starts = Starts(frames=0)
            now = [0.0]
            source = KeyframeSource(lambda: "rtsp://camera.example.test/main", start=starts, clock=lambda: now[0])
            for second in range(600):
                now[0] = float(second)
                self.assertIsNone(await source.frame(timeout=1))
            await source.close()
            return starts

        starts = run(scenario())
        self.assertLessEqual(len(starts.processes), 60)  # Ten minutes at no more than one start every RESTART_SECONDS (10 s)
        self.assertGreaterEqual(len(starts.processes), 59)  # It does keep trying

    def test_a_camera_that_is_switched_off_costs_an_attempt_and_waits_like_any_other_failure(self):
        calls = []

        def switched_off():
            calls.append(1)
            raise ValueError("Camera is disabled or unavailable")

        async def scenario():
            starts = Starts()
            source = KeyframeSource(switched_off, start=starts)
            answers = [await source.frame(timeout=1) for _ in range(5)]
            await source.close()
            return starts, answers

        starts, answers = run(scenario())
        self.assertEqual(answers, [None] * 5)
        self.assertEqual((len(calls), len(starts.processes)), (1, 0))

    def test_output_that_is_not_a_picture_ends_the_decoder_rather_than_allocate_what_it_claims(self):
        huge = b"BM" + (keyframes.MAX_FRAME_BYTES + 1).to_bytes(4, "little") + bytes(8)

        async def scenario():
            starts = Starts(first=huge)
            source = KeyframeSource(lambda: "rtsp://camera.example.test/main", start=starts)
            frame = await source.frame(timeout=2)
            await asyncio.sleep(0.05)
            ended = starts.processes[0].returncode is not None
            await source.close()
            return frame, ended

        frame, ended = run(scenario())
        self.assertIsNone(frame)
        self.assertTrue(ended)

    def test_a_closed_source_starts_nothing(self):
        async def scenario():
            starts = Starts()
            source = KeyframeSource(lambda: "rtsp://camera.example.test/main", start=starts)
            await source.close()
            return starts, await source.frame(timeout=1)

        starts, frame = run(scenario())
        self.assertIsNone(frame)
        self.assertEqual(starts.processes, [])


class AnalysisTests(unittest.TestCase):
    def test_the_ai_worker_keeps_one_source_per_camera_and_closes_it_when_the_cameras_analysis_ends(self):
        from app import detection

        async def scenario():
            starts = Starts()
            manager = detection.DetectionManager.__new__(detection.DetectionManager)
            manager._keyframes = {}
            manager.stopping = False
            with patch.object(detection, "_camera_source", lambda camera_id, substream: f"rtsp://{camera_id}.example.test/main"), \
                    patch.object(keyframes, "start_decoder", starts):
                first = await manager.full_frame("cam-1")
                second = await manager.full_frame("cam-1")
                other = await manager.full_frame("cam-2")
                source = manager._keyframes["cam-1"]

                async def analysis_that_ends(*args, **kwargs):
                    manager.stopping = True
                    return True

                manager._attribute_model = manager._reid_model = manager._clip_model = _none
                manager._analyse_stream = analysis_that_ends
                await manager._run("cam-1", {"provider": "CPU"}, None)
                await manager._keyframes["cam-2"].close()
            return starts, first, second, other, source, manager

        starts, first, second, other, source, manager = run(scenario())
        self.assertTrue(all(frame is not None for frame in (first, second, other)))
        self.assertEqual(starts.addresses, ["rtsp://cam-1.example.test/main", "rtsp://cam-2.example.test/main"])
        self.assertNotIn("cam-1", manager._keyframes)
        self.assertFalse(source.open)
        self.assertIsNotNone(starts.processes[0].returncode)


async def _none(*args, **kwargs):
    return None


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is needed")
class FfmpegTests(unittest.TestCase):
    """The real command against a generated file: full-size pictures, keyframes only, none repeated."""

    def generated(self, directory: str, size: str) -> str:
        path = Path(directory) / "camera.mp4"
        # 45 frames with a keyframe every 5th: 9 keyframes, one a second
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=5:duration=9", "-g", "5", "-keyint_min", "5",
                        "-sc_threshold", "0", "-pix_fmt", "yuv420p", str(path)], check=True)
        return f"file:{path}"  # Not a camera: the recorder's network-only protocols would refuse it, and a bare path is read relative to "pipe:"

    def test_the_frame_is_the_streams_own_size_whatever_its_shape(self):
        for size, shape in (("1920x1080", (1080, 1920, 3)), ("640x480", (480, 640, 3)), ("1000x562", (562, 1000, 3))):
            with tempfile.TemporaryDirectory() as directory:
                address = self.generated(directory, size)

                async def scenario():
                    source = KeyframeSource(lambda: address, start=lambda a: keyframes.start_decoder(a, protocols="concat,file,pipe"))
                    frame = await source.frame(timeout=10)
                    await source.close()
                    return frame

                frame = run(scenario())
            self.assertIsNotNone(frame, size)
            self.assertEqual(frame.shape, shape)
            self.assertGreater(int(frame.max()), 100)  # A picture, not an empty frame

    def test_only_keyframes_are_handed_over_at_most_one_every_two_and_a_half_seconds_and_none_is_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            address = self.generated(directory, "320x240")
            from app.recording import _manifest_bytes
            result = subprocess.run(keyframes.decoder_arguments("ffmpeg", "concat,file,pipe"), input=_manifest_bytes(address), capture_output=True,
                                    timeout=30, check=True)
        # Keyframes at 0 to 8 s: those at 0, 3 and 6 s are handed over. 320 x 240 x 3 + 54 bytes each.
        self.assertEqual(result.stdout.count(b"BM\x36\x84\x03\x00"), 3)
        self.assertEqual(len(result.stdout), 3 * (320 * 240 * 3 + 54))

    def test_the_camera_login_never_reaches_the_command_line(self):
        # Anyone on the host can read a process's arguments (ps, /proc); the address goes over stdin like the recorder's (#272).
        address = "rtsp://viewer:Plate-Secret-77@192.0.2.40:554/main"
        seen: dict[str, object] = {}
        real = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            seen["args"] = args
            process = await real(sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                                                           stdin=kwargs["stdin"], stdout=kwargs["stdout"])
            return process

        async def scenario():
            with patch.object(keyframes.asyncio, "create_subprocess_exec", spawn):
                process = await keyframes.start_decoder(address)
            seen["stdin"] = await process.stdout.read()
            await process.wait()

        run(scenario())
        command = " ".join(seen["args"])
        self.assertNotIn("Plate-Secret-77", command)
        self.assertNotIn("192.0.2.40", command)
        arguments = list(seen["args"])
        self.assertNotIn("file", arguments[arguments.index("-protocol_whitelist") + 1].split(","))
        self.assertEqual(arguments[arguments.index("-i") + 1], "pipe:0")
        self.assertIn(address.encode(), seen["stdin"])


class EndedWithTheWorkerTests(unittest.TestCase):
    def test_closing_a_source_ends_a_real_decoder_that_never_finishes(self):
        real = asyncio.create_subprocess_exec
        started = []

        async def never_finishes(*args, **kwargs):
            process = await real(sys.executable, "-c", "import time\ntime.sleep(60)\n", **kwargs)
            started.append(process)
            return process

        async def scenario():
            source = KeyframeSource(lambda: "rtsp://camera.example.test/main")
            with patch.object(keyframes.asyncio, "create_subprocess_exec", never_finishes), patch.object(keyframes.shutil, "which", return_value="ffmpeg"):
                waiting = asyncio.ensure_future(source.frame(timeout=20))
                for _ in range(500):
                    if started:
                        break
                    await asyncio.sleep(0.01)
                await source.close()
                return await waiting

        self.assertIsNone(run(scenario()))
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].returncode)


if __name__ == "__main__":
    unittest.main()
