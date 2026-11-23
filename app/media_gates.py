"""How much FFmpeg work a signed-in person can start through playback (#490).

A snapshot started one FFmpeg per request and frame stepping one ffprobe, with no limit on how many at once, per person or in all; frame
stepping also held a server thread for the length of its ffprobe, so forty at once took every thread synchronous routes have and every
other ordinary request waited. Converted playback queued without bound, and a render went on after its requester had gone.

``PROCESSES`` is the gate snapshots and frame stepping share: at most ``PROCESSES_IN_ALL`` run at once and ``PROCESSES_EACH`` for one
person. A request waits up to ``WAIT_SECONDS`` for a place and is then refused with 429. ``CONVERSIONS_EACH`` bounds how many converted
or masked recordings one person may have being prepared or waiting.

The counters live on the web server's event loop, the only place they are changed; they are plain numbers, not a semaphore tied to one
loop, so a test's own loop and the server's are the same to them.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Hashable

PROCESSES_IN_ALL = 4
PROCESSES_EACH = 2
WAIT_SECONDS = 2.0
CONVERSIONS_EACH = 3
RETRY_AFTER_SECONDS = 2


class Busy(Exception):
    """No place for this request now: what to tell the person."""


class Gate:
    def __init__(self, in_all: int, each: int, wait: float = WAIT_SECONDS, what: str = "requests") -> None:
        self.in_all, self.each, self.wait, self.what = in_all, each, wait, what
        self.running = 0
        self.by_person: dict[Hashable, int] = {}

    @asynccontextmanager
    async def admit(self, person: Hashable) -> AsyncIterator[None]:
        """A place for one process of ``person``'s, waiting up to ``wait`` seconds for one; Busy if none comes."""
        deadline = time.monotonic() + self.wait
        while self.running >= self.in_all or self.by_person.get(person, 0) >= self.each:
            if time.monotonic() >= deadline:
                if self.by_person.get(person, 0) >= self.each:
                    raise Busy(f"You already have {self.each} {self.what} running: wait for one to finish")
                raise Busy(f"{self.in_all} {self.what} are running: try again in a moment")
            await asyncio.sleep(0.05)
        self.running += 1
        self.by_person[person] = self.by_person.get(person, 0) + 1
        try:
            yield
        finally:
            self.running -= 1
            left = self.by_person[person] - 1
            if left:
                self.by_person[person] = left
            else:
                del self.by_person[person]


class Count:
    """How many of something each person has under way, at most ``each``: no waiting, Busy at once past it."""

    def __init__(self, each: int, what: str) -> None:
        self.each, self.what = each, what
        self.by_person: dict[Hashable, int] = {}

    @asynccontextmanager
    async def admit(self, person: Hashable) -> AsyncIterator[None]:
        if self.by_person.get(person, 0) >= self.each:
            raise Busy(f"You already have {self.each} {self.what} being prepared: wait for one to finish")
        self.by_person[person] = self.by_person.get(person, 0) + 1
        try:
            yield
        finally:
            left = self.by_person[person] - 1
            if left:
                self.by_person[person] = left
            else:
                del self.by_person[person]


PROCESSES = Gate(PROCESSES_IN_ALL, PROCESSES_EACH, what="snapshots or frame steps")
CONVERSIONS = Count(CONVERSIONS_EACH, "converted recordings")
