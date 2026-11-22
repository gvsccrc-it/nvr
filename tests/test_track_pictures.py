"""An object's first picture is a file on the recordings volume, not a blob in the database (#512)."""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import forecast, prometheus, track_pictures, tracks
from app.store import connect

from tests.scene_case import SceneTestCase

AT = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
WIDTH, HEIGHT = 64, 36


def picture(colour: str = "white") -> bytes:
    return subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={colour}:s={WIDTH}x{HEIGHT}",
                           "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"], capture_output=True, check=True).stdout


class TrackPictureTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temporary.name) / "recordings"
        self.jpeg = picture()

    def track(self, at: datetime = AT) -> str:
        track = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'m','1','CPU')",
                       (track, self.camera_id, "person", at.isoformat(), at.isoformat()))
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                       "VALUES(?,?,?,?,0.9,0.1,0.1,0.2,0.2,'m','1','CPU')", (self.camera_id, track, at.isoformat(), "person"))
        return track

    def row(self, track: str):
        with connect() as db:
            return db.execute("SELECT * FROM track_keyframes WHERE track_id=?", (track,)).fetchone()

    def settle(self) -> None:
        """Let the notes be old enough to act on, as a minute passing would."""
        with connect() as db:
            db.execute("UPDATE track_picture_removals SET removed_at='2026-01-01T00:00:00.000Z'")

    def test_the_picture_is_a_file_in_the_cameras_folder_and_the_database_holds_only_where_it_is(self):
        track = self.track()
        self.assertTrue(tracks.store_keyframe(track, AT, self.jpeg))
        row = self.row(track)
        self.assertEqual(row["relative_path"], f"{self.camera_id}/pictures/20261021/{track}.jpg")
        self.assertEqual((bytes(row["jpeg"]), row["size_bytes"], row["camera_id"]), (b"", len(self.jpeg), self.camera_id))
        self.assertEqual((self.root / row["relative_path"]).read_bytes(), self.jpeg)
        self.assertEqual(track_pictures.read(row), self.jpeg)
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM track_picture_removals").fetchone())  # The note taken before the write is gone
            self.assertEqual(track_pictures.totals(db), {self.camera_id: {"files": 1, "bytes": len(self.jpeg)}})
        # A second picture for the same object changes nothing: the first is the one kept.
        self.assertFalse(tracks.store_keyframe(track, AT, picture("black")))
        self.assertEqual(self.row(track)["size_bytes"], len(self.jpeg))
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM track_picture_removals").fetchone())  # And its file is not on the list to delete

    def test_the_thumbnail_and_the_alarm_picture_read_the_file_with_the_privacy_mask_drawn(self):
        from app.push import thumbnail

        track, alarm = self.track(), str(uuid.uuid4())
        tracks.store_keyframe(track, AT, self.jpeg)
        saved = self.client.put(f"/api/cameras/{self.camera_id}/privacy", json={"zones": [{"x": 0, "y": 0, "width": 0.5, "height": 1}]})
        self.assertEqual(saved.status_code, 200, saved.text)
        with connect() as db:
            db.execute("INSERT INTO alarms(id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (alarm, "Rule", self.camera_id, track, "person", "zone_entry", AT.isoformat(), "{}",
                                                          AT.isoformat(), AT.isoformat(), AT.isoformat(), AT.isoformat()))
        response = self.client.get(f"/api/ai/tracks/{track}/thumbnail")
        self.assertEqual(response.status_code, 200, response.text)
        for masked in (response.content, thumbnail(alarm)):
            self.assertTrue(masked.startswith(b"\xff\xd8"))
            self.assertNotEqual(masked, self.jpeg)  # Never the file as it is
            left, right = self.halves(masked)
            self.assertLess(left, 40)  # The masked half is black
            self.assertGreater(right, 200)
        # The file gone (its disk failed, say): no picture, and no error.
        (self.root / self.row(track)["relative_path"]).unlink()
        self.assertEqual(self.client.get(f"/api/ai/tracks/{track}/thumbnail").status_code, 404)
        self.assertIsNone(thumbnail(alarm))

    def halves(self, jpeg: bytes) -> tuple[float, float]:
        raw = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                             input=jpeg, capture_output=True, check=True).stdout
        rows = [raw[index * WIDTH:(index + 1) * WIDTH] for index in range(HEIGHT)]
        left = sum(sum(row[:WIDTH // 2 - 4]) for row in rows) / (HEIGHT * (WIDTH // 2 - 4))
        right = sum(sum(row[WIDTH // 2 + 4:]) for row in rows) / (HEIGHT * (WIDTH // 2 - 4))
        return left, right

    def test_a_picture_stored_before_this_is_still_read_from_its_row(self):
        track = self.track()
        with connect() as db:
            db.execute("INSERT INTO track_keyframes(track_id,captured_at,jpeg) VALUES(?,?,?)", (track, AT.isoformat(), self.jpeg))
            self.assertEqual(track_pictures.totals(db), {})  # Not a file: not counted as one
        self.assertEqual(track_pictures.read(self.row(track)), self.jpeg)
        self.assertEqual(self.client.get(f"/api/ai/tracks/{track}/thumbnail").status_code, 200)
        with connect() as db:
            db.execute("DELETE FROM ai_detections WHERE track_id=?", (track,))
            self.assertIsNone(db.execute("SELECT 1 FROM track_picture_removals").fetchone())  # Nothing on disk to delete

    def test_the_file_goes_when_the_objects_detections_expire(self):
        from app.tracks import purge_expired_detections

        gone, kept = self.track(AT), self.track(AT + timedelta(days=40))
        for track, at in ((gone, AT), (kept, AT + timedelta(days=40))):
            tracks.store_keyframe(track, at, self.jpeg)
        gone_file, kept_file = (self.root / self.row(track)["relative_path"] for track in (gone, kept))
        with connect() as db:
            purge_expired_detections(db, self.camera_id, AT + timedelta(days=30))  # Retention: the first object's detections expire
        self.assertIsNone(self.row(gone))
        self.assertTrue(gone_file.is_file())  # Noted, not yet deleted
        self.assertEqual(track_pictures.sweep(), {"removed": 0, "waiting": 0})  # A note younger than a minute is left: a file may be mid-write
        self.settle()
        self.assertEqual(track_pictures.sweep(), {"removed": 1, "waiting": 0})
        self.assertFalse(gone_file.exists())
        self.assertFalse(gone_file.parent.exists())  # The day's folder went with its last picture
        self.assertTrue(kept_file.is_file())
        with connect() as db:
            self.assertEqual(track_pictures.totals(db)[self.camera_id], {"files": 1, "bytes": len(self.jpeg)})
            self.assertIsNone(db.execute("SELECT 1 FROM track_picture_removals").fetchone())

    def test_deleting_a_camera_notes_its_pictures_for_removal(self):
        track = self.track()
        tracks.store_keyframe(track, AT, self.jpeg)
        file = self.root / self.row(track)["relative_path"]
        with connect() as db:
            db.execute("DELETE FROM cameras WHERE id=?", (self.camera_id,))  # Its tracks and their rows go with it
            self.assertEqual([row[0] for row in db.execute("SELECT relative_path FROM track_picture_removals")], [f"{self.camera_id}/pictures/20261021/{track}.jpg"])
        self.settle()
        self.assertEqual(track_pictures.sweep()["removed"], 1)
        self.assertFalse(file.exists())

    def test_a_worker_killed_between_the_file_and_its_row_leaves_a_file_the_sweep_deletes(self):
        track = self.track()
        with patch.object(track_pictures, "write_file", side_effect=lambda *args: (_ for _ in ()).throw(SystemExit("killed"))):
            with self.assertRaises(SystemExit):
                tracks.store_keyframe(track, AT, self.jpeg)
        relative = f"{self.camera_id}/pictures/20261021/{track}.jpg"
        (self.root / relative).parent.mkdir(parents=True)
        (self.root / relative).write_bytes(self.jpeg)  # The file had been written when the worker died
        self.assertIsNone(self.row(track))
        self.settle()
        self.assertEqual(track_pictures.sweep()["removed"], 1)
        self.assertFalse((self.root / relative).exists())

    def test_a_picture_on_a_disk_that_is_not_there_waits_and_is_not_forgotten(self):
        from app.storage import StorageUnavailable

        track = self.track()
        tracks.store_keyframe(track, AT, self.jpeg)
        file = self.root / self.row(track)["relative_path"]
        with connect() as db:
            db.execute("DELETE FROM ai_detections WHERE track_id=?", (track,))
        self.settle()
        with patch("app.storage._hot_file_path", side_effect=StorageUnavailable("not mounted")):
            self.assertEqual(track_pictures.sweep(), {"removed": 0, "waiting": 1})
        self.assertTrue(file.is_file())
        self.assertEqual(track_pictures.sweep()["removed"], 1)  # The disk is back
        self.assertFalse(file.exists())

    def test_only_a_picture_of_ours_is_ever_read_or_deleted(self):
        segment = self.root / self.camera_id / "footage.mp4"
        segment.parent.mkdir(parents=True, exist_ok=True)
        segment.write_bytes(b"footage")
        outside = Path(self.temporary.name) / "outside.jpg"
        outside.write_bytes(self.jpeg)
        with connect() as db:
            for path in (f"{self.camera_id}/footage.mp4", "../outside.jpg", f"{self.camera_id}/pictures/../footage.mp4", "/etc/hostname"):
                db.execute("INSERT INTO track_picture_removals(relative_path,removed_at) VALUES(?,'2026-01-01T00:00:00.000Z')", (path,))
        track_pictures.sweep()
        self.assertTrue(segment.is_file())
        self.assertTrue(outside.is_file())
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM track_picture_removals").fetchone())  # Not ours: dropped from the list, nothing deleted
        for path in (f"{self.camera_id}/footage.mp4", "../outside.jpg"):
            self.assertIsNone(track_pictures.read({"relative_path": path, "jpeg": b""}))

    def test_an_idle_sweep_writes_nothing_and_the_pictures_are_counted_in_the_storage_figures(self):
        statements: list[str] = []
        real = track_pictures.connect

        class Watched:
            def __enter__(self_inner):
                self_inner.context = real()
                db = self_inner.context.__enter__()
                db.set_trace_callback(statements.append)
                return db

            def __exit__(self_inner, *args):
                return self_inner.context.__exit__(*args)

        with patch.object(track_pictures, "connect", Watched):
            self.assertEqual(track_pictures.sweep(), {"removed": 0, "waiting": 0})
        self.assertTrue(statements)
        self.assertFalse([statement for statement in statements if statement.lstrip().split()[0].upper() in {"BEGIN", "DELETE", "INSERT", "UPDATE"}], statements)
        tracks.store_keyframe(self.track(), AT, self.jpeg)
        with connect() as db:
            measured = {camera["id"]: camera for camera in forecast.measure(db, datetime.now(timezone.utc))}
        self.assertEqual((measured[self.camera_id]["pictures_bytes"], measured[self.camera_id]["stored_bytes"]), (len(self.jpeg), len(self.jpeg)))
        text = prometheus.render(False)
        self.assertIn(f"nvr_track_picture_bytes {len(self.jpeg)}", text)
        self.assertIn("nvr_track_picture_files 1", text)

    def test_a_volume_that_cannot_be_written_to_costs_the_picture_and_nothing_else(self):
        track = self.track()
        with patch("app.storage.camera_recordings_dir", side_effect=OSError("Assigned recording volume is not mounted")):
            with self.assertLogs("app.track_pictures", "WARNING"):
                self.assertFalse(tracks.store_keyframe(track, AT, self.jpeg))
        self.assertIsNone(self.row(track))
        self.assertEqual(os.listdir(self.root / self.camera_id) if (self.root / self.camera_id).exists() else [], [])


class DatabaseSizeTests(SceneTestCase):
    """The status page and /metrics say how big the database is and which tables make it so (#512)."""

    def test_the_largest_tables_are_measured_after_the_nightly_check_and_shown(self):
        from app import db_size, self_test

        before = self.client.get("/api/system/status").json()["database_size"]
        self.assertGreater(before["bytes"], 0)
        self.assertIsNone(before["tables"])  # Not measured yet: the page says when it will be
        self.assertNotIn("nvr_database_table_bytes", prometheus.render(False))
        with connect() as db:
            db.execute("CREATE TABLE big_for_the_test(payload BLOB)")
            db.execute("INSERT INTO big_for_the_test VALUES(zeroblob(3000000))")
        with patch.object(self_test, "due", lambda: True), patch.object(self_test, "patrol", lambda: True):
            self_test.SelfTestManager.process_once()  # The nightly run: the integrity check, then the measurement
        shown = self.client.get("/api/system/status").json()["database_size"]
        self.assertEqual(shown["tables"][0]["name"], "big_for_the_test")
        self.assertGreaterEqual(shown["tables"][0]["bytes"], 3_000_000)
        self.assertLessEqual(len(shown["tables"]), db_size.TABLES_KEPT)
        self.assertIsNotNone(shown["measured_at"])
        # A table's indexes are counted with it, and everything adds up to about the file.
        total = sum(table["bytes"] for table in shown["tables"]) + shown["other_bytes"] + shown["free_bytes"]
        self.assertAlmostEqual(total, shown["bytes"], delta=shown["bytes"] * 0.05 + 65536)
        self.assertIn('nvr_database_table_bytes{table="big_for_the_test"}', prometheus.render(False))

    def test_a_report_that_cannot_be_read_is_no_report(self):
        from app import db_size

        db_size._report_path().write_text("{not json", encoding="utf-8")
        self.assertIsNone(db_size.report()["tables"])
        with patch.object(db_size, "connect", side_effect=__import__("sqlite3").OperationalError("no such table: dbstat")):
            self.assertIsNone(db_size.measure())  # An SQLite built without it: said in the log, nothing else
