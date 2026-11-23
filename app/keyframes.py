"""A full-size frame of a camera, from one decoder that stays open while frames are wanted (#518).

Plate and face reading want a full-resolution picture of a camera every few seconds while a vehicle or person in view still lacks a read. Taking
each as a fresh snapshot started a new FFmpeg and a new camera session every time: about seven process starts and camera sign-ins a second across
twenty busy cameras, on top of the recorder's session, the AI worker's decoder and any live viewers.

A ``KeyframeSource`` instead keeps one FFmpeg per camera open while frames are being asked for. It decodes only the stream's keyframes
(``-skip_frame nokey``), at the stream's own size, and converts one every ``FRAME_EVERY`` seconds of stream to a BMP for Python: a 4K picture
is about 25 MB, and a camera with a keyframe every second would otherwise send that every second. ``frame()`` returns the next picture to arrive
after it is called, as a fresh snapshot did, and nothing is decoded in Python for a picture that nobody is waiting for.

The decoder is closed once nobody has asked for a frame for ``IDLE_SECONDS``, so a quiet camera holds no extra session. It is closed at once when
the camera's analysis stops. A decoder that ended, or that could not be started, is not started again for ``RESTART_SECONDS``, which bounds how
many processes a camera can start a minute, however it fails.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Any, Awaitable, Callable

import numpy as np

from .processes import end_process

logger = logging.getLogger(__name__)

IDLE_SECONDS = 30.0  # A decoder nobody has asked for a frame in this long is closed; plate and face attempts come every 3 s while wanted
RESTART_SECONDS = 10.0  # A decoder is never started again sooner than this after the last start, so at most six starts a minute per camera
WAIT_SECONDS = 12.0  # How long one request waits for the next keyframe, as long as a fresh snapshot was given
FRAME_EVERY = 2.5  # Seconds of stream between the keyframes converted and handed over: just under the 3 s between plate and face attempts
MAX_FRAME_BYTES = 8192 * 4608 * 3 + 64 * 1024  # Larger than any camera's picture: a header claiming more ends the decoder rather than allocate it
BMP_HEADER_BYTES = 14


def decoder_arguments(ffmpeg: str, protocols: str) -> list[str]:
    """The FFmpeg command for a camera's keyframes. The camera's address goes over stdin, never on the command line (#272)."""
    return [ffmpeg, "-v", "error", "-threads", "1", "-skip_frame", "nokey", "-f", "concat", "-safe", "0", "-protocol_whitelist", protocols,
            "-i", "pipe:0", "-an", "-map", "0:v:0", "-vf", f"select='isnan(prev_selected_t)+gte(abs(t-prev_selected_t)\\,{FRAME_EVERY})'",
            "-fps_mode", "passthrough", "-f", "image2pipe", "-c:v", "bmp", "-pix_fmt", "bgr24", "pipe:1"]


async def start_decoder(source: str, protocols: str | None = None) -> "asyncio.subprocess.Process | None":
    """Start FFmpeg on a camera's stream; None when there is no FFmpeg. ``protocols`` is for tests that read a generated file: a camera's stream is
    only ever opened with the recorder's network protocols."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    from .recording import _PROTOCOLS, _manifest_bytes  # Here, not at the top: recording pulls in much more than this needs

    process = await asyncio.create_subprocess_exec(*decoder_arguments(ffmpeg, protocols or _PROTOCOLS), stdin=asyncio.subprocess.PIPE,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    assert process.stdin is not None
    try:
        process.stdin.write(_manifest_bytes(source))
        await process.stdin.drain()
        process.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass  # FFmpeg ended at once; the reader sees the end of its output
    return process


class KeyframeSource:
    """One camera's full-size keyframes. ``source`` gives the camera's stream address (blocking: it reads the database, so it runs in a thread);
    ``start`` starts the decoder for an address. Tests pass a stand-in ``start`` and ``clock``."""

    def __init__(self, source: Callable[[], str], *, start: Callable[[str], Awaitable[Any]] | None = None, idle: float = IDLE_SECONDS,
                 restart: float = RESTART_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self.source, self.start, self.idle, self.restart, self.clock = source, start, idle, restart, clock
        self.starts = 0  # Decoders started, for the tests and for anyone measuring
        self._process: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._waiting: list[asyncio.Future[np.ndarray | None]] = []
        self._wanted = float("-inf")
        self._started = float("-inf")
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def open(self) -> bool:
        return self._reader is not None and not self._reader.done()

    async def frame(self, timeout: float = WAIT_SECONDS) -> np.ndarray | None:
        """The next keyframe to arrive, at the stream's own size (BGR, read-only), or None when none came in ``timeout`` seconds or the decoder
        cannot be started now."""
        if self._closed:
            return None
        self._wanted = self.clock()
        if not await self._running():
            return None
        waiter: asyncio.Future[np.ndarray | None] = asyncio.get_running_loop().create_future()
        self._waiting.append(waiter)
        try:
            return await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if waiter in self._waiting:
                self._waiting.remove(waiter)

    async def close(self) -> None:
        """Stop the decoder for good; anyone waiting gets None."""
        self._closed = True
        reader = self._reader
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        await end_process(self._process, 3)
        self._answer(None)

    async def _running(self) -> bool:
        async with self._lock:  # Plate and face reading on one camera ask at once: one decoder for both
            if self.open:
                return True
            if self.clock() - self._started < self.restart:
                return False
            self._started = self.clock()
            self.starts += 1
            try:
                address = await asyncio.to_thread(self.source)
                process = await (self.start or start_decoder)(address)
            except Exception as exc:  # noqa: BLE001 - a camera that is off or has no address costs this attempt, never the analysis
                logger.debug("A camera's keyframe decoder could not start: %s", type(exc).__name__)
                return False
            if process is None:
                return False
            self._process = process
            self._reader = asyncio.create_task(self._read(process), name="keyframes")
            return True

    async def _read(self, process: Any) -> None:
        from .plates import decode_bmp

        stdout = process.stdout
        try:
            while True:
                # A camera that sends nothing, or a decoder no one wants any more, is closed rather than left holding a session.
                header = await asyncio.wait_for(stdout.readexactly(BMP_HEADER_BYTES), self.idle)
                size = int.from_bytes(header[2:6], "little")
                if header[:2] != b"BM" or not BMP_HEADER_BYTES < size <= MAX_FRAME_BYTES:
                    break
                body = await asyncio.wait_for(stdout.readexactly(size - BMP_HEADER_BYTES), WAIT_SECONDS)
                if self._waiting:
                    self._answer(decode_bmp(header + body))
                if self.clock() - self._wanted >= self.idle:
                    break
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            self._answer(None)
            await end_process(process, 3)

    def _answer(self, frame: np.ndarray | None) -> None:
        waiting, self._waiting = self._waiting, []
        for waiter in waiting:
            if not waiter.done():
                waiter.set_result(frame)
