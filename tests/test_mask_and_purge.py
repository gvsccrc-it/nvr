"""Search thumbnails of a masked camera are kept and their drawing limited, and the AI retention pass takes no write lock with nothing due (#522)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import privacy, tracks
from app.store import connect, initialize

ZONES = [{"x": 0.1, "y": 0.1, "width": 0.3, "height": 0.3}]
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


class MaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        privacy._masked.clear()
        privacy._masked_bytes = 0
        self.addCleanup(privacy._masked.clear)
        self.drawn: list[bytes] = []
        self.running = self.most = 0

        async def ffmpeg(args, stdin=None, timeout=120):
            self.drawn.append(stdin)
            self.running += 1
            self.most = max(self.most, self.running)
            await asyncio.sleep(0.01)
            self.running -= 1
            return b"\xff\xd8masked " + stdin

        patcher = mock.patch.object(privacy, "_ffmpeg", ffmpeg)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_second_request_for_the_same_picture_starts_no_process(self):
        first = await privacy.mask_jpeg(b"picture", ZONES)
        second = await privacy.mask_jpeg(b"picture", ZONES)
        self.assertEqual((first, second, len(self.drawn)), (b"\xff\xd8masked picture", first, 1))

    async def test_a_mask_change_draws_the_picture_again(self):
        await privacy.mask_jpeg(b"picture", ZONES)
        await privacy.mask_jpeg(b"picture", [{**ZONES[0], "width": 0.5}])
        await privacy.mask_jpeg(b"picture", ZONES + [{"x": 0.6, "y": 0.6, "width": 0.1, "height": 0.1}])
        self.assertEqual(len(self.drawn), 3)

    async def test_a_page_of_thumbnails_draws_a_few_at_a_time(self):
        await asyncio.gather(*(privacy.mask_jpeg(f"picture {number}".encode(), ZONES) for number in range(200)))
        self.assertEqual((len(self.drawn), self.most), (200, privacy.MASK_RENDERS))

    async def test_the_same_picture_asked_for_at_once_is_drawn_once(self):
        answers = await asyncio.gather(*(privacy.mask_jpeg(b"picture", ZONES) for _ in range(20)))
        self.assertEqual(len(set(answers)), 1)
        self.assertLessEqual(len(self.drawn), privacy.MASK_RENDERS)  # Those already waiting when the first finished find it kept

    async def test_a_mask_that_cannot_be_drawn_gives_no_picture_and_is_tried_again(self):
        with mock.patch.object(privacy, "_ffmpeg", side_effect=RuntimeError("FFmpeg could not apply the privacy mask")):
            with self.assertRaises(RuntimeError):
                await privacy.mask_jpeg(b"picture", ZONES)
        self.assertEqual(await privacy.mask_jpeg(b"picture", ZONES), b"\xff\xd8masked picture")

    async def test_a_camera_without_masks_starts_no_process(self):
        self.assertEqual(await privacy.mask_jpeg(b"picture", []), b"picture")
        self.assertEqual(self.drawn, [])

    async def test_the_kept_pictures_stay_within_their_memory(self):
        with mock.patch.object(privacy, "MASKED_KEPT_BYTES", 64 * 1024):
            for number in range(100):
                await privacy.mask_jpeg(f"{number:04}".encode() + b"x" * 4000, ZONES)
            self.assertLessEqual(privacy._masked_bytes, 64 * 1024)
            self.assertEqual(privacy._masked_bytes, sum(len(picture) for picture in privacy._masked.values()))
            await privacy.mask_jpeg(b"0099" + b"x" * 4000, ZONES)  # The newest is still kept
        self.assertEqual(len(self.drawn), 100)


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = mock.patch.dict(os.environ, {"NVR_DB_PATH": str(Path(self.temporary.name) / "state.sqlite3")})
        environment.start()
        self.addCleanup(environment.stop)
        initialize()
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES('c1','One',X'01','disabled'),('c2','Two',X'01','disabled')")
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES('t1','c1','car',?,?,'m','1','CPU')",
                       ((NOW - timedelta(days=60)).isoformat(), NOW.isoformat()))

    def detections(self, count: int, *, days_ago: float, plates: bool = False) -> None:
        """``count`` detections (and plate reads), a second apart, ending ``days_ago`` days ago."""
        end = NOW - timedelta(days=days_ago)
        with connect() as db:
            for number in range(count):
                at = (end - timedelta(seconds=number)).isoformat()
                db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                           "VALUES('c1','t1',?,'car',0.9,0,0,0.1,0.1,'m','1','CPU')", (at,))
                if plates:
                    db.execute("INSERT INTO plate_reads(id,camera_id,track_id,plate,folded,confidence,read_at,box_json,reader) VALUES(?,'c1','t1',?,?,0.9,?,'{}','r')",
                               (f"p{days_ago}-{number}", f"P{days_ago}{number}", f"P{days_ago}{number}", at))  # One read per plate and track

    def count(self, table: str) -> int:
        with connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_a_pass_with_nothing_due_takes_no_write_lock(self):
        self.detections(50, days_ago=1, plates=True)  # All within retention
        statements: list[str] = []
        with connect() as db:
            db.set_trace_callback(statements.append)
            self.assertEqual(tracks.purge_expired_detections(db, "c1", NOW - timedelta(days=30)), 0)
            tracks.purge_expired_detections(db, "c2", NOW - timedelta(days=30))  # No detections at all
        self.assertFalse([statement for statement in statements if any(word in statement for word in ("BEGIN", "DELETE", "UPDATE", "INSERT"))], statements)

    def test_a_large_purge_is_removed_in_batches_and_plate_reads_go_by_the_same_rule(self):
        self.detections(2_500, days_ago=40, plates=True)  # Expired
        self.detections(20, days_ago=1, plates=True)  # Kept
        with connect() as db:  # Expired, but footage still covers them: kept
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES('c1','c1/a.mp4',?,?,1)",
                       ((NOW - timedelta(days=40, seconds=10)).isoformat(), (NOW - timedelta(days=40) + timedelta(seconds=1)).isoformat()))
        statements: list[str] = []
        with connect() as db:
            db.set_trace_callback(statements.append)
            removed = tracks.purge_expired_detections(db, "c1", NOW - timedelta(days=30))
        self.assertEqual(removed, 2_500 - 11)  # 11 under the footage, 0 to 10 seconds before its end
        self.assertEqual((self.count("ai_detections"), self.count("plate_reads")), (31, 31))
        writes = [statement for statement in statements if statement.startswith("BEGIN")]
        self.assertEqual(len(writes), 6)  # Three batches of at most PURGE_BATCH for each table
        self.assertLessEqual(max(json.loads(statement.split("json_each('")[1].split("')")[0]).__len__() for statement in statements
                                 if statement.startswith("DELETE")), tracks.PURGE_BATCH)

    def test_the_retention_pass_finds_the_cameras_with_tracks_through_an_index(self):
        with connect() as db:
            plan = " ".join(row[3] for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM cameras c WHERE EXISTS (SELECT 1 FROM ai_tracks t WHERE t.camera_id=c.id)"))
        self.assertNotIn("SCAN t", plan)
        self.assertNotIn("SCAN ai_tracks", plan)


if __name__ == "__main__":
    unittest.main()
