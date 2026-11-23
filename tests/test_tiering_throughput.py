"""Tiering keeps up with the cameras on a volume, and says when it does not (#486).

One thread moved at most 20 segments a policy and then slept 30 s: 40 a minute at most, while 200 cameras record 200. Each move read the
file back from the tier in full, which for an object store is a billed download. The candidate query scanned and sorted the whole table on
every pass, nothing measured the backlog, and the rate limit was a fresh allowance for every segment."""
from __future__ import annotations

import json
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import tiering
from app.recording import recordings_path
from app.store import connect
from tests.test_tiering import TierCase

NOW = datetime.now(timezone.utc)


class DrainTests(TierCase):
    def many(self, count: int, age_days: float = 40) -> list[int]:
        """``count`` small segments of camera d1, oldest first, written in one go."""
        folder = recordings_path() / self.d1
        folder.mkdir(parents=True, exist_ok=True)
        start = NOW - timedelta(days=age_days)
        rows = []
        for number in range(count):
            name = f"{uuid.uuid4()}.mp4"
            (folder / name).write_bytes(b"small segment %d" % number)
            began = start + timedelta(seconds=number)
            rows.append((self.d1, f"{self.d1}/{name}", began.isoformat(), (began + timedelta(seconds=1)).isoformat(), len(b"small segment %d" % number)))
        with connect() as db:
            db.executemany("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)", rows)
            return [row[0] for row in db.execute("SELECT id FROM recording_segments WHERE camera_id=? ORDER BY ends_at", (self.d1,))]

    def test_a_backlog_of_10000_small_segments_drains_at_well_over_40_a_minute(self):
        self.many(10_000)
        self.policy(self.local_tier())  # The tests' policies allow 1,000,000 KiB a second: in effect, no limit
        started = time.monotonic()
        outcomes, waiting = tiering.drain(until=started + 6.0)
        took = time.monotonic() - started
        per_minute = outcomes.get("moved", 0) / took * 60
        self.assertEqual(set(outcomes), {"moved"})
        self.assertGreater(per_minute, 400, f"{outcomes.get('moved', 0)} moved in {took:.1f} s")  # Ten times the old ceiling at the least
        with connect() as db:
            left = db.execute("SELECT COUNT(*) FROM recording_segments WHERE tier_id IS NULL").fetchone()[0]
        self.assertEqual(waiting, left > 0)  # Footage still waiting: the next pass starts at once

    def test_the_next_pass_starts_at_once_while_footage_is_waiting_and_waits_when_none_is(self):
        manager = tiering.TieringManager(poll_seconds=30.0)
        pauses: list[float] = []

        async def scenario():
            import asyncio

            real_sleep = asyncio.sleep

            async def sleep(seconds):
                pauses.append(seconds)
                if len(pauses) >= 3:
                    raise asyncio.CancelledError
                await real_sleep(0)

            with mock.patch.object(manager, "pass_once", side_effect=[True, True, False]), mock.patch("asyncio.sleep", sleep):
                try:
                    await manager._run()
                except asyncio.CancelledError:
                    pass

        import asyncio

        asyncio.run(scenario())
        self.assertEqual(pauses, [0, 0, 30.0])

    def test_a_stop_ends_a_long_pass_after_the_segment_being_moved(self):
        import threading

        self.many(50)
        self.policy(self.local_tier())
        stop = threading.Event()
        real = tiering.move_segment
        moved: list[int] = []

        def move(segment_id, *args, **kwargs):
            moved.append(segment_id)
            if len(moved) == 3:
                stop.set()  # The worker is asked to stop while this one moves
            return real(segment_id, *args, **kwargs)

        with mock.patch.object(tiering, "move_segment", move):
            outcomes, waiting = tiering.drain(until=time.monotonic() + 60, stop=stop)
        self.assertEqual((outcomes, waiting), ({"moved": 3}, True))

    def test_one_batch_still_moves_one_batch_when_no_deadline_is_given(self):
        self.many(30)
        self.policy(self.local_tier())
        self.assertEqual(tiering.run_once(), {"moved": tiering.BATCH})

    def test_a_tier_that_fails_ends_the_pass_instead_of_trying_every_segment(self):
        self.many(50)
        self.policy(self.local_tier())
        with mock.patch.object(tiering.LocalStore, "put", side_effect=OSError("the disk went away")):
            outcomes, waiting = tiering.drain(until=time.monotonic() + 30)
        self.assertEqual((outcomes, waiting), ({"failed": tiering.BATCH}, False))


class CandidatePlanTests(TierCase):
    def test_the_candidate_query_reads_the_due_index_in_order_without_a_full_scan_or_a_sort(self):
        with connect() as db:
            plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + tiering.CANDIDATES_SQL, ("2026-01-01", "default", "2026-01-01", "x%", "2026-01-01", "2026-01-01", 20))]
        self.assertTrue(any("recording_segments_tier_due_idx" in step and step.startswith("SEARCH s") for step in plan), plan)
        self.assertFalse([step for step in plan if step.startswith("SCAN s") or "TEMP B-TREE" in step], plan)


class RateTests(unittest.TestCase):
    def test_the_rate_is_in_kibibytes_a_second_which_the_page_shows_as_mib_a_second(self):
        clock = [100.0]
        slept: list[float] = []

        def sleep(seconds):
            slept.append(seconds)
            clock[0] += seconds

        with mock.patch("app.tiering.time.monotonic", lambda: clock[0]), mock.patch("app.tiering.time.sleep", sleep):
            rate = tiering.Rate(1024)  # 1 MiB a second, which the page shows as "1" in "Rate limit (MiB per second)"
            for _ in range(10):
                rate.wait(1024 * 1024)
        self.assertAlmostEqual(sum(slept), 9.0, places=3)  # Ten MiB in ten seconds: one second's allowance at the start, then one a second

    def test_the_limit_holds_across_moves_and_idle_time_is_not_saved_up(self):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch("app.tiering.time.monotonic", lambda: clock[0]), mock.patch("app.tiering.time.sleep", sleep):
            first = tiering.rate_for("default", 64)
            for _ in range(64):  # Many small moves, each with its own few chunks, share the one allowance
                tiering.rate_for("default", 64).wait(16 * 1024)
            self.assertIs(tiering.rate_for("default", 64), first)
            self.assertAlmostEqual(clock[0], 15.0, places=3)  # 1 MiB at 64 KiB a second: 16 s, less the first second's allowance
            clock[0] += 3600  # An idle hour
            started = clock[0]
            for _ in range(16):
                tiering.rate_for("default", 64).wait(16 * 1024)
            self.assertAlmostEqual(clock[0] - started, 3.0, places=3)  # Only a second's allowance kept: 256 KiB took 3 s, not none
            self.assertIsNot(tiering.rate_for("default", 128), first)  # A changed limit starts afresh
        tiering._rates.clear()

    def test_one_large_chunk_owes_what_it_did_not_wait_for(self):
        slept: list[float] = []
        clock = [0.0]

        def sleep(seconds):
            slept.append(seconds)
            clock[0] += seconds

        with mock.patch("app.tiering.time.monotonic", lambda: clock[0]), mock.patch("app.tiering.time.sleep", sleep):
            rate = tiering.Rate(64)
            rate.wait(1024 * 1024)  # 16 s of data: at most 5 s waited at a time, so a stop is noticed
            rate.wait(1)
            rate.wait(1)
        self.assertEqual(slept[0], 5.0)
        self.assertAlmostEqual(sum(slept), 15.0, places=2)  # What was owed was waited for by the calls after


class ConfirmationTests(TierCase):
    def requests(self, method: str) -> int:
        return sum(1 for entry in self.stub.requests if entry[0] == method and entry[2] == 200)

    def test_a_bucket_copy_is_confirmed_by_the_stores_checksum_with_no_download(self):
        segment_id, payload = self.segment(self.d1, 40)
        tier = self.s3_tier()
        self.policy(tier)
        self.assertEqual(tiering.run_once(), {"moved": 1})
        self.assertEqual(self.requests("GET"), 0)
        self.assertGreaterEqual(self.requests("HEAD"), 1)
        self.assertEqual(self.stub.objects[f"nvr/{self.row(segment_id)['relative_path']}"], payload)
        view = next(item for item in self.admin.get("/api/admin/tiering").json()["tiers"] if item["id"] == tier)
        self.assertEqual((view["checksums"], view["confirmed_by"], view["read_back"]), ("yes", "checksum", False))

    def test_a_checksum_that_does_not_match_keeps_the_hot_copy(self):
        segment_id, payload = self.segment(self.d1, 40)
        self.policy(self.s3_tier())
        self.stub.wrong_checksums = True
        self.assertEqual(tiering.run_once(), {"failed": 1})
        self.assertIsNone(self.row(segment_id)["tier_id"])
        self.assertEqual(self.hot(segment_id).read_bytes(), payload)
        with connect() as db:
            self.assertIn("did not match", db.execute("SELECT last_error FROM tier_moves WHERE segment_id=?", (segment_id,)).fetchone()[0])

    def test_a_store_that_keeps_no_checksums_is_read_back_in_full_and_the_page_says_so(self):
        segment_id, _ = self.segment(self.d1, 40)
        tier = self.s3_tier()
        self.policy(tier)
        self.stub.checksums = False
        self.assertEqual(tiering.run_once(), {"moved": 1})
        self.assertEqual(self.requests("GET"), 1)  # The full read-back
        view = next(item for item in self.admin.get("/api/admin/tiering").json()["tiers"] if item["id"] == tier)
        self.assertEqual((view["checksums"], view["confirmed_by"]), ("no", "read_back_no_checksums"))
        self.assertIsNotNone(self.row(segment_id)["tier_id"])

    def test_a_read_back_from_a_store_that_keeps_no_checksums_still_refuses_a_changed_copy(self):
        segment_id, _ = self.segment(self.d1, 40)
        self.policy(self.s3_tier())
        self.stub.checksums, self.stub.corrupt_gets = False, True
        self.assertEqual(tiering.run_once(), {"failed": 1})
        self.assertIsNone(self.row(segment_id)["tier_id"])

    def test_the_administrator_can_have_every_file_read_back_from_a_bucket(self):
        self.segment(self.d1, 40)
        tier = self.s3_tier()
        self.policy(tier)
        changed = self.admin.put(f"/api/admin/tiering/tiers/{tier}/read-back", json={"read_back": True})
        self.assertEqual(changed.status_code, 200, changed.text)
        view = next(item for item in changed.json()["tiers"] if item["id"] == tier)
        self.assertEqual((view["read_back"], view["confirmed_by"]), (True, "read_back_chosen"))
        self.assertEqual(tiering.run_once(), {"moved": 1})
        self.assertEqual(self.requests("GET"), 1)
        self.assertEqual(self.admin.put("/api/admin/tiering/tiers/nope/read-back", json={"read_back": True}).status_code, 404)
        self.assertEqual(self.full.put(f"/api/admin/tiering/tiers/{tier}/read-back", json={"read_back": False}).status_code, 403)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='cold_tier_read_back_changed'").fetchone()[0], 1)

    def test_a_folder_tier_is_always_read_back(self):
        segment_id, _ = self.segment(self.d1, 40)
        tier = self.local_tier()
        self.policy(tier)
        with mock.patch.object(tiering.LocalStore, "read_digest", autospec=True, side_effect=tiering.LocalStore.read_digest) as read:
            self.assertEqual(tiering.run_once(), {"moved": 1})
        self.assertEqual(read.call_count, 1)
        view = next(item for item in self.admin.get("/api/admin/tiering").json()["tiers"] if item["id"] == tier)
        self.assertEqual(view["confirmed_by"], "read_back")


class BacklogTests(TierCase):
    def measure(self, at: datetime) -> dict:
        return tiering.measure_backlog(at, force=True)

    def incident(self):
        with connect() as db:
            return db.execute("SELECT state,summary FROM incidents WHERE incident_key='tiering-backlog:default'").fetchone()

    def test_the_page_shows_how_many_segments_wait_and_the_oldest(self):
        oldest, _ = self.segment(self.d1, 45)
        self.segment(self.d1, 40)
        self.segment(self.d1, 5)  # Not due yet
        held, _ = self.segment(self.d1, 50)
        self.policy(self.local_tier())
        response = self.admin.post(f"/api/cameras/{self.d1}/segments/{held}/hold", json={})
        self.assertIn(response.status_code, (200, 201), response.text)
        self.measure(NOW)
        backlog = self.admin.get("/api/admin/tiering").json()["backlog"]["default"]
        self.assertEqual(backlog["segments"], 2)  # Held footage never moves: it is not a backlog
        self.assertEqual(backlog["oldest_ends_at"], self.row(oldest)["ends_at"])
        self.assertFalse(backlog["growing"])

    def test_a_backlog_that_keeps_growing_raises_an_incident_and_one_that_shrinks_recovers_it(self):
        self.policy(self.local_tier())
        self.segment(self.d1, 40)
        at = NOW
        for _ in range(tiering.BACKLOG_SAMPLES):
            self.segment(self.d1, 33)  # More falls due than moves
            self.measure(at)
            at += tiering.BACKLOG_EVERY
        incident = self.incident()
        self.assertEqual(incident["state"], "active")
        self.assertIn("falling behind", incident["summary"])
        self.assertTrue(self.admin.get("/api/admin/tiering").json()["backlog"]["default"]["growing"])
        tiering.run_once(at, limit=100)
        self.measure(at)
        self.assertEqual(self.incident()["state"], "recovered")

    def test_a_backlog_that_rises_and_falls_raises_nothing(self):
        self.policy(self.local_tier())
        at = NOW
        for step in range(tiering.BACKLOG_SAMPLES + 3):
            self.segment(self.d1, 40)
            if step % 2:
                tiering.run_once(at, limit=100)  # Keeping up, between measurements
            self.measure(at)
            at += tiering.BACKLOG_EVERY
        self.assertIsNone(self.incident())

    def test_a_growing_backlog_of_footage_only_just_due_is_not_yet_an_incident(self):
        self.policy(self.local_tier(), after_days=30)
        at = NOW
        for _ in range(tiering.BACKLOG_SAMPLES):
            self.segment(self.d1, 30.1)  # Due, but within the day the next passes are given
            self.measure(at)
            at += tiering.BACKLOG_EVERY
        self.assertIsNone(self.incident())

    def test_measurements_are_spaced_and_kept_to_an_hour(self):
        self.policy(self.local_tier())
        self.segment(self.d1, 40)
        tiering.measure_backlog(NOW)
        self.assertEqual(tiering.measure_backlog(NOW + timedelta(minutes=1)), {})  # Not again so soon
        at = NOW
        for _ in range(tiering.BACKLOG_SAMPLES + 5):
            at += tiering.BACKLOG_EVERY
            tiering.measure_backlog(at)
        with connect() as db:
            samples = json.loads(db.execute("SELECT samples_json FROM tier_backlog").fetchone()[0])
        self.assertEqual(len(samples), tiering.BACKLOG_SAMPLES)

    def test_a_policy_turned_off_is_not_measured_and_its_incident_recovers(self):
        self.policy(self.local_tier())
        self.segment(self.d1, 40)
        at = NOW
        for _ in range(tiering.BACKLOG_SAMPLES):
            self.segment(self.d1, 33)
            self.measure(at)
            at += tiering.BACKLOG_EVERY
        self.assertEqual(self.incident()["state"], "active")
        with connect() as db:
            db.execute("UPDATE tier_policies SET enabled=0")
        self.measure(at)
        self.assertEqual(self.incident()["state"], "recovered")
        self.assertEqual(self.admin.get("/api/admin/tiering").json()["backlog"], {})


if __name__ == "__main__":
    unittest.main()
