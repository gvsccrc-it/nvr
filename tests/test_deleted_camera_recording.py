"""A recording whose camera's row is deleted stops, and its footage is never indexed onto a later camera given the same id.

The recorder's five-second pass synced only the cameras in the table, so a recording whose camera was deleted by another process (a standby's
sync of the primary's cameras, or a test fixture) ran for good. Its segments could not be indexed (the camera's row was gone) and were kept to
be tried again every five seconds, until a camera with that id existed again: then they were indexed onto it. A browser journey saw this as
footage on a camera that had none (main, after PR #594)."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


class DeletedCameraTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(self.root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(self.root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(self.root / "credentials.key")
        from app.credentials import encrypt
        from app.store import connect, initialize

        initialize()
        self.camera_id = str(uuid.uuid4())
        self.address = encrypt("rtsp://camera.invalid/main")
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled,recording_mode,recording_status) VALUES(?,?,?,0,'continuous','stopped')",
                       (self.camera_id, "Gone", self.address))

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def delete_camera(self):
        from app.store import connect
        with connect() as db:
            db.execute("DELETE FROM cameras WHERE id=?", (self.camera_id,))

    def test_a_recording_whose_camera_was_deleted_is_stopped_by_the_next_pass(self):
        from app.recording import RecordingManager

        async def scenario():
            manager = RecordingManager()
            recording = asyncio.create_task(asyncio.sleep(3600))  # Stands in for the camera's recording
            manager.tasks[self.camera_id], manager.task_modes[self.camera_id] = recording, "continuous"
            self.delete_camera()
            real = asyncio.sleep

            async def one_pass(seconds, *args, **kwargs):
                if seconds == 5:
                    manager.stopping = True
                    seconds = 0
                return await real(seconds, *args, **kwargs)

            with patch("app.recording.asyncio.sleep", one_pass):
                await manager._schedule_loop()
            return manager, recording

        manager, recording = asyncio.run(scenario())
        self.assertTrue(recording.cancelled())
        self.assertNotIn(self.camera_id, manager.tasks)

    def test_the_last_segments_of_a_deleted_camera_are_not_kept_for_a_camera_given_its_id_later(self):
        from app import recording
        from app.store import connect

        manager = recording.RecordingManager()
        run = self.root / "recordings" / self.camera_id / "run"
        run.mkdir(parents=True)
        (run / "segment-20261123T065200Z.mp4").write_bytes(b"\0" * 4096)  # As FFmpeg names and lists a finished segment
        listing = run / "segments.csv"
        listing.write_text("segment-20261123T065200Z.mp4,0.000000,60.000000\n")
        self.delete_camera()
        with self.assertLogs("nvr.recording", "WARNING") as logged:
            manager._index_rest(self.camera_id, listing, run)
        self.assertEqual(manager._unindexed, [])  # Not tried again
        self.assertIn("no longer exists", logged.output[0])
        self.assertTrue((run / "segment-20261123T065200Z.mp4").exists())  # The file is left where it is

        with connect() as db:  # A camera is given the same id (a fixture does this; ids are random otherwise)
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled,recording_mode,recording_status) VALUES(?,?,?,0,'continuous','stopped')",
                       (self.camera_id, "New", self.address))
        manager._index_leftovers()
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recording_segments WHERE camera_id=?", (self.camera_id,)).fetchone()[0], 0)

    def test_a_database_that_cannot_be_written_still_keeps_the_segments_to_try_again(self):
        from app import recording

        manager = recording.RecordingManager()
        run = self.root / "recordings" / self.camera_id / "run"
        run.mkdir(parents=True)
        listing = run / "segments.csv"
        with patch.object(manager, "_index_csv", side_effect=sqlite3.OperationalError("database is locked")):
            manager._index_rest(self.camera_id, listing, run)
        self.assertEqual(manager._unindexed, [(self.camera_id, listing, run)])  # #483 as before


if __name__ == "__main__":
    unittest.main()
