"""The camera health pass and the hardening checks stay bounded at 200 cameras or with many offline (#477)."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import timedelta
from unittest import mock

from app import camera_health, hardening
from app.credentials import encrypt
from app.store import connect
from tests import test_camera_health, test_hardening
from tests.test_camera_health import NOW
from tests.test_hardening import Offers


class HealthPassTests(test_camera_health.HealthCase):
    def recording_cameras(self, count: int) -> list[str]:
        cameras = []
        with connect() as db:
            for index in range(count):
                camera = str(uuid.uuid4())
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,recording_enabled) VALUES(?,?,X'01','continuous',1)", (camera, f"Busy {index:03d}"))
                cameras.append(camera)
        return cameras

    def test_with_fifty_cameras_due_only_a_few_are_measured_at_once(self):
        self.recording_cameras(50)
        guard, running, most = threading.Lock(), [0], [0]

        def slow(db, camera_id, now):
            with guard:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.05)
            with guard:
                running[0] -= 1
            return {}

        with mock.patch.object(camera_health, "measure_recordings", slow):
            self.sample()
        self.assertEqual(most[0], camera_health.MEASURE_CONCURRENCY)

    def test_a_stalled_measurement_holds_up_only_its_own_camera(self):
        stalled, *others = self.recording_cameras(9)
        release = threading.Event()

        def measure(db, camera_id, now):
            if camera_id == stalled:
                release.wait(10)  # A volume that has stopped answering
            return {}

        async def run() -> list[str]:
            pass_ = asyncio.create_task(camera_health.sample_once(NOW))
            deadline = time.monotonic() + 5
            measured: list[str] = []
            while time.monotonic() < deadline and len(measured) < len(others):
                await asyncio.sleep(0.05)
                with connect() as db:
                    measured = [row[0] for row in db.execute("SELECT camera_id FROM camera_health WHERE measured_at IS NOT NULL") if row[0] in others]
            release.set()
            await pass_
            return measured

        with mock.patch.object(camera_health, "measure_recordings", measure):
            measured = asyncio.run(run())
        self.assertEqual(sorted(measured), sorted(others))  # All measured while the stalled one still waited

    def test_cameras_checked_together_come_due_apart_and_stay_apart(self):
        cameras = self.recording_cameras(60)
        with mock.patch.object(camera_health, "measure_recordings", return_value={}):
            self.sample(NOW)  # Everyone at once, as at start-up
        due_at = sorted(camera_health.sample_seconds(camera) for camera in cameras)
        self.assertGreaterEqual(due_at[0], camera_health.SAMPLE_SECONDS - camera_health.SAMPLE_SPREAD_SECONDS / 2)
        self.assertLess(due_at[-1], camera_health.SAMPLE_SECONDS + camera_health.SAMPLE_SPREAD_SECONDS / 2)
        # Passes a minute apart over the following minutes: each finds some, none finds all
        found = []
        with connect() as db:
            for minute in range(7, 14):
                found.append({row["id"] for row in camera_health.due(db, NOW + timedelta(minutes=minute)) if row["id"] in cameras})
        firsts = [len(found[0])] + [len(later - earlier) for earlier, later in zip(found, found[1:])]
        self.assertEqual(len(found[-1]), 60)
        self.assertLess(max(firsts), 30, firsts)  # No pass gets more than half of them for the first time
        self.assertGreaterEqual(sum(1 for count in firsts if count), 4, firsts)  # Spread over at least four passes


class HardeningRunTests(test_hardening.HardeningCase):
    def offline(self, count: int) -> list[str]:
        cameras = []
        with connect() as db:
            for index in range(count):
                camera = str(uuid.uuid4())
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,?,'disabled')",
                           (camera, f"Offline {index:02d}", encrypt(f"rtsp://viewer:Zq9-long-unique-passphrase@10.20.1.{index + 1}:554/main")))
                cameras.append(camera)
        return cameras

    def unanswered(self, delay: float):
        def prober(host: str, kind: str):
            time.sleep(delay)
            raise hardening.Unreachable()
        return prober

    def wait_until_done(self, deadline: float = 60) -> dict:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            body = self.admin.get("/api/hardening").json()
            if not body["run"]["running"]:
                return body
            time.sleep(0.2)
        self.fail("the run did not finish")

    def test_run_checks_answers_within_seconds_and_shows_progress_and_who_did_not_answer(self):
        cameras = self.offline(12)
        with mock.patch("app.hardening.tls_offered", self.unanswered(1.0)):
            began = time.monotonic()
            answer = self.admin.post("/api/hardening/run")
            took = time.monotonic() - began
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertLess(took, 5, f"the request was held {took:.1f} s")
            run = answer.json()["run"]
            self.assertTrue(run["running"])
            self.assertGreater(run["probes_total"], 0)
            self.assertLess(run["probes_done"], run["probes_total"])
            # Asked again meanwhile, it does not start a second run
            self.assertTrue(self.admin.post("/api/hardening/run").json()["run"]["running"])
            done = self.wait_until_done()
        self.assertEqual(done["run"]["probes_done"], done["run"]["probes_total"])
        self.assertTrue(set(cameras) <= {camera["camera_id"] for camera in done["run"]["not_answered"]})
        self.assertIn("Offline 00", {camera["camera_name"] for camera in done["run"]["not_answered"]})
        self.assertEqual(len(self.audit("hardening_run")), 1)

    def test_saving_the_settings_does_not_wait_for_the_probes(self):
        self.offline(12)
        with mock.patch("app.hardening.tls_offered", self.unanswered(1.0)):
            began = time.monotonic()
            answer = self.admin.put("/api/hardening/settings", json={"camera_networks": ["10.20.1.0/24"]})
            took = time.monotonic() - began
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertLess(took, 5)
            self.assertEqual(answer.json()["camera_networks"], ["10.20.1.0/24"])  # Saved at once
            self.wait_until_done()

    def test_a_quick_run_still_answers_with_its_result(self):
        self.set_camera(self.d1, stream="rtsp://viewer:Zq9-long-unique-passphrase@10.20.0.5:554/main")
        with mock.patch("app.hardening.tls_offered", Offers({("10.20.0.5", "rtsps"): 322})):
            body = self.admin.post("/api/hardening/run").json()
        self.assertFalse(body["run"]["running"])
        self.assertIn("plain_rtsp", self.findings_for(body, self.d1))

    def test_a_run_that_died_with_its_process_does_not_block_the_next_one(self):
        with connect() as db:
            db.execute("UPDATE hardening_settings SET run_started_at=? WHERE id=1", ((hardening._now() - hardening.RUN_STALE - timedelta(minutes=1)).isoformat(),))
        self.assertFalse(self.admin.get("/api/hardening").json()["run"]["running"])
        with mock.patch("app.hardening.tls_offered", Offers()):
            self.assertTrue(hardening.run("manual")["ran"])


if __name__ == "__main__":
    import unittest

    unittest.main()
