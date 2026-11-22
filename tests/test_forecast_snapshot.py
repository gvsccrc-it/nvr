"""The metrics scrape and the forecast pages read the forecast as last worked out, not every recorded segment each time (#460)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import forecast, logforward, prometheus
from app.api import disks as disks_api
from app.store import connect
from tests import test_disk_health
from tests.test_disk_health import GB


def usage(free_gb: float):
    """A disk with this much free, as shutil.disk_usage answers (the scrape reads its total and use too)."""
    import shutil

    return shutil._ntuple_diskusage(2000 * GB, 2000 * GB - int(free_gb * GB), int(free_gb * GB))


def statements_on(db) -> list[str]:
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    return seen


class SnapshotTests(test_disk_health.ForecastCase):
    def footage(self) -> None:
        now = datetime.now(timezone.utc)
        self.write(self.d1, 10, ends=now)
        self.write(self.d2, 5, ends=now)
        self.policy(self.d1, 30)
        self.policy(self.d2, 14)

    def free(self, gb: float = 270):
        return mock.patch("app.forecast.shutil.disk_usage", return_value=usage(gb))

    def test_the_kept_forecast_is_the_forecast(self):
        self.footage()
        now = datetime.now(timezone.utc)
        with self.free(), connect() as db:
            worked_out = forecast.forecast(db, now)
            kept = forecast.refresh(db, now)
            read = forecast.current(db, now + timedelta(minutes=1))
        self.assertEqual(read, worked_out)
        self.assertEqual(kept, worked_out)

    def test_a_reader_adds_up_no_segments_while_the_kept_forecast_is_recent(self):
        self.footage()
        with self.free(), connect() as db:
            forecast.refresh(db)
            seen = statements_on(db)
            forecast.current(db)
            prometheus._disks(prometheus._Output(), db, logforward.Scrubber())
        self.assertFalse([statement for statement in seen if "recording_segments" in statement], seen)

    def test_the_three_readers_use_it_through_the_api_and_the_scrape(self):
        self.footage()
        with self.free(), connect() as db:
            forecast.refresh(db)
        real = disks_api.connect
        seen: list[str] = []

        def traced():
            db = real()
            db.set_trace_callback(seen.append)
            return db

        with self.free(), mock.patch.object(disks_api, "connect", traced):
            self.assertEqual(self.admin.get("/api/storage/forecast").status_code, 200)
            self.assertEqual(self.admin.get(f"/api/cameras/{self.d1}/retention-forecast").status_code, 200)
        self.assertTrue(seen)
        self.assertFalse([statement for statement in seen if "recording_segments" in statement], seen)

    def test_an_old_or_missing_forecast_is_worked_out_again_and_kept(self):
        self.footage()
        now = datetime.now(timezone.utc)
        with self.free(), connect() as db:
            first = forecast.current(db, now)  # Nothing kept yet: worked out now
            self.assertEqual(first["as_of"], now.isoformat())
            self.assertEqual(forecast.current(db, now + timedelta(minutes=9))["as_of"], now.isoformat())  # Still recent: the kept one
            later = now + forecast.SNAPSHOT_MAX_AGE + timedelta(minutes=1)
            self.assertEqual(forecast.current(db, later)["as_of"], later.isoformat())  # Too old: worked out again

    def test_a_camera_added_since_it_was_worked_out_still_gets_its_forecast(self):
        self.footage()
        with self.free(), connect() as db:
            forecast.refresh(db)
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES('fresh','Fresh',X'01','disabled')")
        with self.free():
            answer = self.admin.get("/api/cameras/fresh/retention-forecast")
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["camera"]["id"], "fresh")

    def test_the_recorders_pass_keeps_it(self):
        from app.recording import RecordingManager

        self.footage()
        with self.free(), mock.patch("app.smart.volumes_to_read", return_value=[]), mock.patch("app.smart.due", return_value=False):
            RecordingManager._watch_disks()
        with connect() as db:
            self.assertIsNotNone(db.execute("SELECT computed_at FROM forecast_snapshot").fetchone())

    def test_a_scrape_decrypts_each_stored_value_once_in_the_process(self):
        self.footage()
        logforward._decrypted.clear()
        with mock.patch("app.credentials.decrypt", wraps=__import__("app.credentials", fromlist=["decrypt"]).decrypt) as decrypt, self.free():
            prometheus.render(False)
            first = decrypt.call_count
            prometheus.render(False)
            prometheus.render(False)
        self.assertGreater(first, 0)
        self.assertEqual(decrypt.call_count, first)  # The later scrapes decrypted nothing


class MillionSegmentTests(test_disk_health.ForecastCase):
    def test_a_scrape_with_a_million_segments_stored_takes_under_a_tenth_of_a_second(self):
        # Fifty cameras with fourteen days of one-minute footage: 1,008,000 segments, as the ticket measured
        with connect() as db:
            cameras = [row[0] for row in db.execute("SELECT id FROM cameras")]
            for index in range(len(cameras), 50):
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (f"extra-{index}", f"Extra {index}"))
            db.execute("""
                WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i < 20159)
                INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes)
                SELECT c.id, c.id || '/' || n.i || '.mp4',
                       strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-14 days', '+' || n.i || ' minutes'),
                       strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', '-14 days', '+' || (n.i + 1) || ' minutes'), 4000000
                FROM n, (SELECT id FROM cameras ORDER BY id LIMIT 50) c""")
            self.assertGreaterEqual(db.execute("SELECT COUNT(*) FROM recording_segments").fetchone()[0], 1_000_000)
        with mock.patch("app.forecast.shutil.disk_usage", return_value=usage(500)):
            with connect() as db:
                began = time.perf_counter()
                forecast.refresh(db)  # The recorder's pass: the one place the segments are added up
                working_out = time.perf_counter() - began
            prometheus.render(False)  # Warm, as a running server is
            began = time.perf_counter()
            text = prometheus.render(False)
            took = time.perf_counter() - began
        self.assertIn("nvr_camera_retention_forecast_days", text)
        self.assertLess(took, 0.1, f"a scrape took {took * 1000:.0f} ms (working the forecast out takes {working_out * 1000:.0f} ms)")


if __name__ == "__main__":
    import unittest

    unittest.main()
