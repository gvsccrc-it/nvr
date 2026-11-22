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


if __name__ == "__main__":
    unittest.main()
