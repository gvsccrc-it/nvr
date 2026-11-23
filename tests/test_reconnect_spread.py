"""Cameras that drop together do not all reconnect in the same second (#495).

The wait before a reconnect depended only on the number of failures, so cameras that failed together (a switch rebooting, a power cut)
retried together at +2, +4, +8, +16, +30 s for the whole outage, each starting an FFmpeg and a test connection and writing its status in the
same second, and all connected in the same second when the network came back. Start-up launched every camera in one pass, and the
keep-ready retries followed one fixed ladder."""
from __future__ import annotations

import asyncio
import collections
import random
import unittest
from unittest import mock

from app import recording
from app.recording import RECONNECT_MAX_SECONDS, reconnect_delay

CAMERAS = 200


def attempts_per_second(seed: int, minutes: float = 10) -> collections.Counter:
    """When 200 cameras that failed at the same moment, and go on failing, try again: a stepped clock, one camera's waits after another."""
    rng = random.Random(seed)
    counts: collections.Counter = collections.Counter()
    for _ in range(CAMERAS):
        clock, failures = 0.0, 0
        while clock < minutes * 60:
            failures += 1
            clock += reconnect_delay(failures, rng)
            if failures > 1:  # After the first retry
                counts[int(clock)] += 1
    return counts


class ReconnectSpreadTests(unittest.TestCase):
    def test_two_hundred_cameras_failing_at_once_do_not_retry_in_the_same_second(self):
        for seed in range(5):
            counts = attempts_per_second(seed)
            self.assertLessEqual(max(counts.values()), 75, f"seed {seed}: {max(counts.values())} attempts in one second")  # Was all 200
            settled = [count for second, count in counts.items() if second >= 120]
            self.assertLessEqual(max(settled), 25, f"seed {seed}: once at the longest wait, {max(settled)} in one second")

    def test_one_cameras_first_retry_is_still_within_two_to_three_seconds(self):
        rng = random.Random(1)
        first = [reconnect_delay(1, rng) for _ in range(1000)]
        self.assertGreaterEqual(min(first), 2.0)
        self.assertLessEqual(max(first), 3.0)
        later = [reconnect_delay(20, rng) for _ in range(1000)]
        self.assertGreaterEqual(min(later), RECONNECT_MAX_SECONDS * 0.5)
        self.assertLessEqual(max(later), RECONNECT_MAX_SECONDS * 1.5)
        self.assertAlmostEqual(sum(later) / len(later), RECONNECT_MAX_SECONDS, delta=1.5)  # The same on average as before

    def test_start_up_launches_cameras_in_bounded_batches(self):
        started: list[tuple[str, float]] = []
        clock = {"now": 0.0}

        async def scenario():
            manager = recording.RecordingManager()
            cameras = [{"id": f"camera-{number}"} for number in range(40)]

            async def sync(camera_id, force_restart=False):
                started.append((camera_id, clock["now"]))

            async def pause(seconds):
                clock["now"] += seconds

            class Db:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, *_args):
                    return mock.Mock(fetchall=lambda: cameras)

            with mock.patch.object(manager, "sync_camera", sync), mock.patch.object(manager, "_mark_restart_gaps"), \
                    mock.patch.object(recording, "connect", lambda: Db()), mock.patch.object(recording.viewing_audit, "close_orphans"), \
                    mock.patch.object(manager, "_sweep_empty_run_folders", return_value=0), mock.patch.object(recording.asyncio, "sleep", pause), \
                    mock.patch.object(recording.asyncio, "create_task", lambda coroutine, name=None: coroutine.close()), \
                    mock.patch.object(manager.tamper, "start", mock.AsyncMock()):
                await manager.start()

        asyncio.run(scenario())
        self.assertEqual(len(started), 40)
        by_moment = collections.Counter(moment for _, moment in started)
        self.assertLessEqual(max(by_moment.values()), recording.STARTUP_BATCH)
        self.assertEqual(len(by_moment), 3)  # 16, 16 and 8, half a second apart


if __name__ == "__main__":
    unittest.main()
