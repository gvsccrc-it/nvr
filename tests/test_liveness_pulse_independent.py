"""A database that cannot be written, with every camera's write queued behind it, does not stop a worker's sign of life (#482).

The pulse file is what the supervisor reads. It used to be written in the same loop as the status row, whose write queued in the shared
thread pool behind every camera's stalled write: the pulse went 60 s without being written and the supervisor killed a healthy recorder."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.store import connect, initialize
from app.workers import liveness

_ENV = ("NVR_DB_PATH", "NVR_CONTROL_DIR")
BEAT = 0.05
STALL = 1.5


class PulseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_CONTROL_DIR"] = str(Path(self.temporary.name) / "run")
        initialize()
        liveness.record("recorder", state="starting", pid=os.getpid(), started=True)
        for name, value in (("HEARTBEAT_SECONDS", BEAT), ("BEAT_BUSY_SECONDS", 0.2)):
            patcher = patch.object(liveness, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def heartbeat_at(self) -> str | None:
        with connect() as db:
            return db.execute("SELECT heartbeat_at FROM worker_status WHERE name='recorder'").fetchone()[0]

    def camera_write(self) -> None:
        """One camera's status write, as the recorder's watch does it: in the shared thread pool, waiting for the database's lock."""
        db = sqlite3.connect(os.environ["NVR_DB_PATH"], timeout=STALL + 1)
        try:
            db.execute("UPDATE worker_status SET updated_at=updated_at WHERE name='nobody'")
            db.commit()
        except sqlite3.Error:
            pass
        finally:
            db.close()

    def test_the_pulse_keeps_coming_while_the_database_is_locked_and_the_thread_pool_is_full(self):
        async def scenario():
            heartbeat = asyncio.create_task(liveness.heartbeat_forever("recorder"))
            await asyncio.sleep(BEAT * 4)
            self.assertIsNotNone(self.heartbeat_at(), "the status row is written while the database is free")
            holder = sqlite3.connect(os.environ["NVR_DB_PATH"], isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")  # One long write somewhere: nothing else can write
            before = self.heartbeat_at()
            cameras = [asyncio.create_task(asyncio.to_thread(self.camera_write)) for _ in range(200)]  # Far more than the pool has threads
            oldest = 0.0
            started = time.monotonic()
            while time.monotonic() - started < STALL:
                await asyncio.sleep(BEAT)
                oldest = max(oldest, time.time() - liveness.pulse_path("recorder").stat().st_mtime)
                self.assertTrue(liveness.pulse_fresh("recorder", os.getpid()))
            during = self.heartbeat_at()
            holder.execute("ROLLBACK")
            holder.close()
            await asyncio.gather(*cameras)
            await asyncio.sleep(BEAT * 8)
            after = self.heartbeat_at()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            return oldest, before, during, after

        oldest, before, during, after = asyncio.run(scenario())
        self.assertLess(oldest, BEAT * 8, "the pulse was never older than a few beats")
        self.assertEqual(during, before, "the status row may go stale during the stall")
        self.assertGreater(after, during, "and it is written again afterwards")

    def test_a_blocked_event_loop_still_stops_the_pulse(self):
        async def scenario():
            heartbeat = asyncio.create_task(liveness.heartbeat_forever("recorder"))
            await asyncio.sleep(BEAT * 3)
            time.sleep(0.6)  # The loop itself is stuck: this is what the supervisor restarts a worker for
            age = time.time() - liveness.pulse_path("recorder").stat().st_mtime
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            return age

        self.assertGreater(asyncio.run(scenario()), 0.5)

    def test_cancelling_the_heartbeat_ends_the_status_task_too(self):
        async def scenario():
            heartbeat = asyncio.create_task(liveness.heartbeat_forever("recorder"))
            await asyncio.sleep(BEAT * 2)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await asyncio.sleep(BEAT)
            return [task.get_name() for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]

        self.assertEqual(asyncio.run(scenario()), [])


if __name__ == "__main__":
    unittest.main()
