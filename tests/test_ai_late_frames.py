"""An overloaded AI worker says once that a camera is behind, not on every late frame (#511).

A late frame used to write the camera as "delayed" and the same frame, once stored, as "analyzed" again: two coverage rows, a "recovered"
and a "recurred" incident event, an email and two webhooks per frame, while the one inference slot was held for the write."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import detection
from app.store import connect
from app.tracks import Tracker
from tests.scene_case import SceneTestCase

MODEL = {"model_id": "m", "model_version": "1", "provider": "CPU"}
CONFIG = {"analysis_fps": 1000.0, "confidence_threshold": 0.5, "effective_vehicle_confidence_threshold": 0.5}
T0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)


class Detector:
    def infer(self, frame, threshold):
        return []


class LateFrameTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        self.frame_number = detection.WARM_UP_FRAMES
        for name, value in (("LATE_AFTER_SECONDS", 0.02), ("LATE_FOR_SECONDS", 0.0), ("ON_TIME_FOR_SECONDS", 0.15)):
            patcher = patch.object(detection, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def frame(self, manager: detection.DetectionManager, *, late: bool) -> None:
        """What the frame loop does for one frame: its turn at inference, then storing it."""
        self.frame_number += 1
        if late:
            await manager._inference_slots.acquire()  # Another camera's frame is in the model
            asyncio.get_running_loop().call_later(0.04, manager._inference_slots.release)
        await manager._detect(self.camera_id, CONFIG, Detector(), b"", self.frame_number)
        at = T0 + timedelta(seconds=self.frame_number * 10)  # Past the state's own write intervals: every frame may write
        await manager._db(detection._store_frame, self.camera_id, [], at, Tracker(), None, MODEL, manager._delayed(self.camera_id))

    def counts(self) -> tuple[dict[str, int], int]:
        with connect() as db:
            events = dict(db.execute("SELECT e.event_type, COUNT(*) FROM incident_events e JOIN incidents i ON i.id=e.incident_id WHERE i.incident_key=? GROUP BY 1",
                                     (f"ai_analysis:{self.camera_id}",)).fetchall())
            coverage = db.execute("SELECT COUNT(*) FROM ai_coverage WHERE camera_id=?", (self.camera_id,)).fetchone()[0]
        return events, coverage

    def status(self) -> str:
        with connect() as db:
            return db.execute("SELECT status FROM ai_camera_settings WHERE camera_id=?", (self.camera_id,)).fetchone()[0]

    def test_ten_late_frames_raise_one_incident_and_change_coverage_once(self):
        async def scenario():
            manager = detection.DetectionManager()
            await self.frame(manager, late=False)
            before = self.counts()[1]
            for _ in range(10):
                await self.frame(manager, late=True)
            return before

        before = asyncio.run(scenario())
        events, coverage = self.counts()
        self.assertEqual(events, {"raised": 1})
        self.assertEqual(coverage - before, 1, "one change: analysed, then delayed")
        self.assertEqual(self.status(), "delayed")

    def test_recovery_is_recorded_once_and_only_after_a_while_on_time(self):
        async def scenario():
            manager = detection.DetectionManager()
            for _ in range(3):
                await self.frame(manager, late=True)
            for _ in range(3):
                await self.frame(manager, late=False)
            soon = (self.status(), self.counts()[0])
            await asyncio.sleep(detection.ON_TIME_FOR_SECONDS)
            for _ in range(3):
                await self.frame(manager, late=False)
            return soon

        soon = asyncio.run(scenario())
        self.assertEqual(soon, ("delayed", {"raised": 1}), "three frames on time are not yet a recovery")
        self.assertEqual(self.counts()[0], {"raised": 1, "recovered": 1})
        self.assertEqual(self.status(), "analyzed")

    def test_a_late_frame_now_and_then_is_not_reported(self):
        async def scenario():
            manager = detection.DetectionManager()
            for late in (False, True, False, False, True, False):
                await self.frame(manager, late=late)

        with patch.object(detection, "LATE_FOR_SECONDS", 5.0):
            asyncio.run(scenario())
        self.assertEqual(self.counts()[0], {})
        self.assertEqual(self.status(), "analyzed")

    def test_the_first_frames_of_a_stream_do_not_count(self):
        async def scenario():
            manager = detection.DetectionManager()
            self.frame_number = 0
            for _ in range(detection.WARM_UP_FRAMES):
                await self.frame(manager, late=True)
            return manager

        manager = asyncio.run(scenario())
        self.assertEqual(self.counts()[0], {})
        self.assertEqual(manager.capacity.late, {})

    def test_nothing_waits_for_the_database_while_the_inference_slot_is_held(self):
        held: list[bool] = []

        async def scenario():
            manager = detection.DetectionManager()
            real = manager._db

            async def watched(write, *args):
                held.append(manager._inference_slots.locked())
                return await real(write, *args)

            manager._db = watched
            for _ in range(4):
                await self.frame(manager, late=True)

        asyncio.run(scenario())
        self.assertTrue(held)
        self.assertFalse(any(held))

    def test_a_delayed_camera_that_is_then_stopped_does_not_keep_its_incident(self):
        for state in ("skipped", "disabled"):
            with self.subTest(state=state):
                with connect() as db:
                    detection._set_camera_state(db, self.camera_id, "delayed", detection.DELAYED_REASON, at=T0)
                    self.assertEqual(db.execute("SELECT fault_active FROM incidents WHERE incident_key=?", (f"ai_analysis:{self.camera_id}",)).fetchone()[0], 1)
                    detection._set_camera_state(db, self.camera_id, state, "Waiting for capacity" if state == "skipped" else None, at=T0 + timedelta(minutes=1))
                    self.assertEqual(db.execute("SELECT fault_active FROM incidents WHERE incident_key=?", (f"ai_analysis:{self.camera_id}",)).fetchone()[0], 0)

    def test_a_failed_camera_that_is_then_stopped_keeps_its_incident(self):
        with connect() as db:
            detection._set_camera_state(db, self.camera_id, "failed", "Camera stream ended", at=T0)
            detection._set_camera_state(db, self.camera_id, "skipped", "Waiting for capacity", at=T0 + timedelta(minutes=1))
            self.assertEqual(db.execute("SELECT fault_active FROM incidents WHERE incident_key=?", (f"ai_analysis:{self.camera_id}",)).fetchone()[0], 1)


class LatenessTests(SceneTestCase):
    def test_late_must_last_and_on_time_must_last(self):
        lateness = detection._Lateness()
        with patch.object(detection, "LATE_FOR_SECONDS", 20.0), patch.object(detection, "ON_TIME_FOR_SECONDS", 60.0):
            for now, late, delayed in ((0, True, False), (10, True, False), (19, False, False), (21, True, True), (30, False, True), (60, True, True),
                                       (70, False, True), (129, False, True), (130, False, False), (131, True, False), (151, True, True)):
                lateness.note(late, float(now))
                self.assertEqual(lateness.delayed, delayed, now)
