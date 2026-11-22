"""A camera's changed settings reach its running recording (#466).

FFmpeg's arguments and the camera's address are read once, when a recording starts. Every settings route asked the recorder to restart the
camera, and for a camera that was recording the request did nothing: audio switched off kept being recorded until the stream next dropped.
The recorder now remembers what each running recording was started with and restarts the ones whose inputs have changed, a few at a time."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app import recording
from app.credentials import encrypt
from app.recording import RecordingManager
from app.store import connect, initialize

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_STORAGE_VOLUMES")


class Recorder(RecordingManager):
    """The real manager with the recording itself replaced by a stand-in that says when it was started and stays running."""

    def __init__(self) -> None:
        super().__init__()
        self.started: list[tuple[str, str]] = []
        self.running = 0
        self.fail: set[str] = set()

    async def _run(self, camera_id: str, expected_mode: str) -> None:
        self.started.append((camera_id, expected_mode))
        if camera_id in self.fail:
            raise RuntimeError("the recording task failed")
        await asyncio.Event().wait()


class RestartCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(self.root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.root / "credentials.key")
        os.environ["NVR_STORAGE_VOLUMES"] = json.dumps({"default": "", "archive": "archive"})
        (self.root / "recordings" / "archive").mkdir(parents=True)
        initialize()

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def camera(self, mode: str = "continuous", group: str | None = None) -> str:
        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled,recording_mode,audio_enabled,group_id,motion_fallback_enabled) VALUES(?,?,?,1,?,1,?,1)",
                       (camera_id, f"Camera {camera_id[:8]}", encrypt("rtsp://192.0.2.10/stream"), mode, group))
        return camera_id

    def change(self, camera_id: str, **columns: object) -> None:
        with connect() as db:
            for column, value in columns.items():
                db.execute(f"UPDATE cameras SET {column}=? WHERE id=?", (value, camera_id))

    def run_scenario(self, scenario):
        async def run():
            manager = Recorder()
            try:
                return await scenario(manager)
            finally:
                manager.stopping = True
                for camera_id in list(manager.tasks):
                    await manager.remove(camera_id)

        return asyncio.run(run())


class PinnedTests(RestartCase):
    """What a sync did before the change and still does."""

    def test_a_sync_without_force_leaves_a_healthy_running_recording_alone(self):
        camera_id = self.camera()

        async def scenario(manager):
            await manager.sync_camera(camera_id)
            task = manager.tasks[camera_id]
            await asyncio.sleep(0)
            for _ in range(3):
                await manager.sync_camera(camera_id)
            return task is manager.tasks[camera_id], manager.started

        same, started = self.run_scenario(scenario)
        self.assertTrue(same)
        self.assertEqual(started, [(camera_id, "continuous")])

    def test_a_recording_task_that_failed_is_started_again(self):
        camera_id = self.camera()

        async def scenario(manager):
            manager.fail.add(camera_id)
            await manager.sync_camera(camera_id)
            await asyncio.sleep(0.01)
            manager.fail.clear()
            await manager.sync_camera(camera_id)
            await asyncio.sleep(0)
            return manager.started

        self.assertEqual(self.run_scenario(scenario), [(camera_id, "continuous")] * 2)

    def test_a_change_of_mode_replaces_the_recording(self):
        camera_id = self.camera()

        async def scenario(manager):
            await manager.sync_camera(camera_id)
            await asyncio.sleep(0)
            self.change(camera_id, recording_mode="motion")
            await manager.sync_camera(camera_id)
            await asyncio.sleep(0)
            return manager.started

        self.assertEqual(self.run_scenario(scenario), [(camera_id, "continuous"), (camera_id, "motion")])

    def test_a_camera_switched_off_is_stopped(self):
        camera_id = self.camera()

        async def scenario(manager):
            await manager.sync_camera(camera_id)
            await asyncio.sleep(0)
            self.change(camera_id, recording_enabled=0)
            await manager.sync_camera(camera_id)
            return camera_id in manager.tasks

        self.assertFalse(self.run_scenario(scenario))


class ChangedSettingsTests(RestartCase):
    def setUp(self):
        super().setUp()
        spacing = patch.object(recording, "RESTART_SPACING_SECONDS", 0.0)
        spacing.start()
        self.addCleanup(spacing.stop)

    def restarts_after(self, mode: str, *, force: bool = True, **columns: object) -> int:
        camera_id = self.camera(mode)

        async def scenario(manager):
            await manager.sync_camera(camera_id)
            await asyncio.sleep(0)
            first = manager.tasks[camera_id]
            self.change(camera_id, **columns)
            await manager.sync_camera(camera_id, force_restart=force)
            await asyncio.sleep(0)
            self.assertEqual(first.cancelled(), manager.tasks[camera_id] is not first)
            return len(manager.started) - 1

        return self.run_scenario(scenario)

    def test_audio_switched_off_starts_the_recording_again(self):
        self.assertEqual(self.restarts_after("continuous", audio_enabled=0), 1)

    def test_a_new_address_or_login_starts_the_recording_again(self):
        self.assertEqual(self.restarts_after("continuous", stream_url_ciphertext=encrypt("rtsp://192.0.2.11/stream")), 1)
        self.assertEqual(self.restarts_after("continuous", stream_url_ciphertext=encrypt("rtsp://viewer:rotated@192.0.2.10/stream")), 1)

    def test_the_same_address_saved_again_does_not(self):
        self.assertEqual(self.restarts_after("continuous", stream_url_ciphertext=encrypt("rtsp://192.0.2.10/stream")), 0)

    def test_a_motion_cameras_zones_sensitivity_and_rolls_start_it_again(self):
        for columns in ({"motion_zones_json": "[[0.1,0.1,0.5,0.5]]"}, {"motion_sensitivity": 80}, {"motion_pre_roll_seconds": 20}, {"motion_post_roll_seconds": 5},
                        {"motion_service_url": "http://192.0.2.10/onvif/events"}, {"motion_credentials_ciphertext": encrypt(json.dumps({"username": "a", "password": "b"}))}):
            with self.subTest(columns=list(columns)):
                self.assertEqual(self.restarts_after("motion", **columns), 1)

    def test_a_continuous_cameras_motion_settings_do_not(self):
        self.assertEqual(self.restarts_after("continuous", motion_sensitivity=80, motion_zones_json="[[0.1,0.1,0.5,0.5]]"), 0)

    def test_a_forced_sync_with_nothing_changed_restarts_nothing(self):
        self.assertEqual(self.restarts_after("continuous", name="Renamed"), 0)

    def test_the_change_is_picked_up_without_being_asked(self):
        self.assertEqual(self.restarts_after("continuous", force=False, audio_enabled=0), 1, "the five-second pass applies it even if the request to the recorder was lost")

    def test_the_gap_is_recorded_with_its_reason(self):
        camera_id = self.camera()

        async def scenario(manager):
            await manager.sync_camera(camera_id)
            await asyncio.to_thread(manager._sync_coverage, camera_id)
            self.change(camera_id, audio_enabled=0)
            await manager.sync_camera(camera_id, force_restart=True)

        self.run_scenario(scenario)
        with connect() as db:
            rows = db.execute("SELECT coverage_kind,reason_code,ends_at FROM coverage_intervals WHERE camera_id=? ORDER BY id", (camera_id,)).fetchall()
        self.assertEqual([(row["coverage_kind"], row["reason_code"], row["ends_at"] is None) for row in rows][-1], ("uncertain", "settings_restart", True))
        self.assertTrue(all(row["ends_at"] for row in rows[:-1]), "the interval before it was closed")
        from app import coverage

        self.assertIn("settings_restart", coverage.REASONS)


class GroupTests(RestartCase):
    CAMERAS = 12

    def setUp(self):
        super().setUp()
        with connect() as db:
            db.execute("INSERT INTO camera_groups(id,name) VALUES('yard','Yard')")
            db.execute("INSERT OR IGNORE INTO storage_volumes(id,relative_path) VALUES('archive','archive')")
        self.cameras = [self.camera(group="yard") for _ in range(self.CAMERAS)]

    def test_a_schedule_save_that_changes_no_cameras_mode_restarts_none(self):
        async def scenario(manager):
            await asyncio.gather(*(manager.sync_camera(camera_id) for camera_id in self.cameras))
            await asyncio.sleep(0)
            await asyncio.gather(*(manager.sync_camera(camera_id, force_restart=True) for camera_id in self.cameras))  # What the policy routes do
            await asyncio.sleep(0)
            return len(manager.started)

        self.assertEqual(self.run_scenario(scenario), self.CAMERAS)

    def test_a_volume_change_restarts_the_groups_cameras_a_few_at_a_time(self):
        together = {"now": 0, "most": 0}

        async def scenario(manager):
            await asyncio.gather(*(manager.sync_camera(camera_id) for camera_id in self.cameras))
            await asyncio.sleep(0)
            real = manager._replace

            async def counted(*args):
                together["now"] += 1
                together["most"] = max(together["most"], together["now"])
                try:
                    await real(*args)
                    await asyncio.sleep(0.02)  # A restart takes a moment: FFmpeg is asked to stop and waited for
                finally:
                    together["now"] -= 1

            manager._replace = counted
            with connect() as db:
                db.execute("UPDATE camera_groups SET storage_volume_id='archive' WHERE id='yard'")
            await asyncio.gather(*(manager.sync_camera(camera_id, force_restart=True) for camera_id in self.cameras))
            await asyncio.sleep(0)
            return len(manager.started)

        with patch.object(recording, "RESTART_SPACING_SECONDS", 0.01):
            started = self.run_scenario(scenario)
        self.assertEqual(started, self.CAMERAS * 2, "every camera of the group was started again")
        self.assertEqual(together["most"], recording.RESTARTS_AT_ONCE)

    def test_two_requests_for_one_camera_restart_it_once(self):
        camera_id = self.cameras[0]

        async def scenario(manager):
            await manager.sync_camera(camera_id)
            await asyncio.sleep(0)
            self.change(camera_id, audio_enabled=0)
            with patch.object(recording, "RESTART_SPACING_SECONDS", 0.02):
                await asyncio.gather(*(manager.sync_camera(camera_id, force_restart=True) for _ in range(6)))  # The route's request and the five-second pass, at once
            await asyncio.sleep(0)
            return len(manager.started)

        self.assertEqual(self.run_scenario(scenario), 2)


if __name__ == "__main__":
    unittest.main()
