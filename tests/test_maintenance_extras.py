"""The two maintenance-window additions the owner chose on 24 November 2026 (#234): a reminder to the person who opened a window shortly
before it ends, with a way to extend it, and AI analysis paused on the cameras a window covers, with the pause shown as not analysed."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import detection, maintenance, push, search
from app.detection import DetectionManager
from app.store import connect
from tests.test_maintenance import MaintenanceCase
from tests.test_push import Device, PushService
from tests.test_support import as_given

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


class ReminderCase(MaintenanceCase):
    def window(self, window_id: str):
        with connect() as db:
            return db.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone()

    def ends(self, window_id: str) -> datetime:
        return maintenance.parse(self.window(window_id)["ends_at"])

    def subscribe_admin(self) -> PushService:
        service = PushService()
        self.addCleanup(service.close)
        for target, stand_in in (("app.push.validate_endpoint", lambda endpoint: endpoint), ("app.push.pin_endpoint", as_given)):
            patcher = mock.patch(target, side_effect=stand_in)  # The local stand-in is plain HTTP on loopback
            patcher.start()
            self.addCleanup(patcher.stop)
        self.device = Device()
        response = self.admin.post("/api/push/subscriptions", json={"endpoint": f"{service.origin}/push/phone", "keys": self.device.keys(), "device_label": "phone"})
        self.assertEqual(response.status_code, 201, response.text)
        return service

    def reminders(self) -> list[dict]:
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT alarm_id,maintenance_window_id,payload_json,status FROM push_deliveries WHERE maintenance_window_id IS NOT NULL")]


class ReminderTimeTests(ReminderCase):
    def test_ten_minutes_before_the_end_a_quarter_of_a_short_window_and_none_for_a_very_short_one(self):
        start = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(maintenance.reminder_time(start, start + timedelta(hours=1)), maintenance.iso(start + timedelta(minutes=50)))
        self.assertEqual(maintenance.reminder_time(start, start + timedelta(minutes=20)), maintenance.iso(start + timedelta(minutes=15)))
        self.assertEqual(maintenance.reminder_time(start, start + timedelta(minutes=4)), maintenance.iso(start + timedelta(minutes=3)))
        self.assertIsNone(maintenance.reminder_time(start, start + timedelta(minutes=3)))


class ReminderTests(ReminderCase):
    def test_the_reminder_fires_once_at_its_time_and_not_before(self):
        window = self.open(self.admin, camera_ids=[self.d1], minutes=60).json()
        end = self.ends(window["id"])
        self.assertEqual(maintenance.remind_due(end - timedelta(minutes=11)), [])
        self.assertIsNone(self.window(window["id"])["reminded_at"])
        self.assertFalse(self.admin.get("/api/maintenance").json()["windows"][0]["ending_soon"])
        self.assertEqual(maintenance.remind_due(end - timedelta(minutes=10)), [window["id"]])
        self.assertIsNotNone(self.window(window["id"])["reminded_at"])
        self.assertEqual(maintenance.remind_due(end - timedelta(minutes=9)), [])  # Not twice
        self.assertEqual(maintenance.remind_due(end - timedelta(minutes=1)), [])
        with mock.patch.object(maintenance, "_now", return_value=end - timedelta(minutes=5)):
            self.assertTrue(maintenance.ending_soon(self.window(window["id"])))

    def test_no_reminder_for_a_window_ended_early_or_cancelled_before_it_started_or_over(self):
        ended = self.open(self.admin, camera_ids=[self.d1], minutes=60).json()
        end = self.ends(ended["id"])
        self.assertEqual(self.admin.post(f"/api/maintenance/{ended['id']}/end").status_code, 200)
        later = self.open(self.admin, camera_ids=[self.d2], minutes=60, starts_at=(NOW() + timedelta(hours=2)).isoformat()).json()
        later_end = self.ends(later["id"])
        self.assertEqual(self.admin.post(f"/api/maintenance/{later['id']}/end").json()["state"], "removed")
        self.assertEqual(maintenance.remind_due(end - timedelta(minutes=5)), [])
        self.assertEqual(maintenance.remind_due(later_end - timedelta(minutes=5)), [])
        over = self.open(self.admin, camera_ids=[self.y1], minutes=60).json()
        self.assertEqual(maintenance.remind_due(self.ends(over["id"]) + timedelta(seconds=1)), [])  # Its end has passed: too late to say it is coming
        self.assertEqual(self.reminders(), [])

    def test_extending_sets_the_reminder_again_for_the_new_end(self):
        window = self.open(self.full, camera_ids=[self.d1], minutes=60).json()
        end = self.ends(window["id"])
        self.assertEqual(maintenance.remind_due(end - timedelta(minutes=10)), [window["id"]])
        extended = self.full.post(f"/api/maintenance/{window['id']}/extend", json={"minutes": 60})
        self.assertEqual(extended.status_code, 200, extended.text)
        self.assertEqual(maintenance.parse(extended.json()["ends_at"]), end + timedelta(hours=1))
        self.assertIsNone(self.window(window["id"])["reminded_at"])
        self.assertEqual(maintenance.remind_due(end - timedelta(minutes=5)), [])  # The old end is no longer near
        self.assertEqual(maintenance.remind_due(end + timedelta(minutes=49)), [])
        self.assertEqual(maintenance.remind_due(end + timedelta(minutes=50)), [window["id"]])  # Ten minutes before the new end
        self.assertEqual(maintenance.remind_due(end + timedelta(minutes=55)), [])
        [entry] = self.audit("maintenance_extended")
        self.assertEqual((entry["details"]["minutes"], entry["resource_id"]), (60, window["id"]))

    def test_a_few_minutes_added_inside_the_reminder_time_send_no_second_reminder(self):
        window = self.open(self.admin, camera_ids=[self.d1], minutes=8).json()  # Its reminder time is two minutes before the end: already near
        with mock.patch.object(maintenance, "_now", return_value=self.ends(window["id"]) - timedelta(minutes=1)):
            with connect() as db:
                maintenance.extend(db, window["id"], 1, administrator=True)
        self.assertIsNotNone(self.window(window["id"])["reminded_at"])  # The person extending it has just seen it
        self.assertEqual(maintenance.remind_due(self.ends(window["id"]) - timedelta(seconds=30)), [])

    def test_extending_keeps_to_the_limit_of_opening_a_window_and_to_who_may_end_it(self):
        window = self.open(self.full, camera_ids=[self.d1], minutes=7 * 60).json()
        self.assertEqual(self.full.post(f"/api/maintenance/{window['id']}/extend", json={"minutes": 60}).status_code, 200)  # Eight hours in all
        refused = self.full.post(f"/api/maintenance/{window['id']}/extend", json={"minutes": 1})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("at most 8 hours", refused.json()["detail"])
        self.assertEqual(self.limited.post(f"/api/maintenance/{window['id']}/extend", json={"minutes": 30}).status_code, 404)  # Not theirs
        self.assertEqual(self.admin.post(f"/api/maintenance/{window['id']}/extend", json={"minutes": 30}).status_code, 200)  # An administrator's limit is 7 days
        self.assertEqual(self.full.post(f"/api/maintenance/{window['id']}/end").status_code, 200)
        self.assertEqual(self.full.post(f"/api/maintenance/{window['id']}/extend", json={"minutes": 30}).status_code, 409)  # Already ended

    def test_the_reminder_is_pushed_to_the_devices_of_the_person_who_opened_the_window_only(self):
        service = self.subscribe_admin()
        other = Device()
        self.assertEqual(self.full.post("/api/push/subscriptions", json={"endpoint": f"{service.origin}/push/other", "keys": other.keys(), "device_label": "other"}).status_code, 201)
        window = self.open(self.admin, camera_ids=[self.d1], minutes=60, name="Dock lens").json()
        self.assertEqual(maintenance.remind_due(self.ends(window["id"]) - timedelta(minutes=10)), [window["id"]])
        [queued] = self.reminders()
        self.assertEqual((queued["alarm_id"], queued["maintenance_window_id"]), (None, window["id"]))
        self.assertEqual(push.deliver_due(), ["sent"])
        self.assertEqual([request["path"] for request in service.requests], ["/push/phone"])
        message = self.device.decrypt(service.requests[0]["body"])
        self.assertEqual(message["title"], "Maintenance ends in 10 minutes: Dock lens")
        self.assertIn("extend", message["body"])
        self.assertEqual(message["url"], "/cameras/maintenance")
        self.assertEqual(push.deliver_due(), [])  # Sent once

    def test_an_idle_reminder_pass_takes_no_write_lock(self):
        self.open(self.admin, camera_ids=[self.d1], minutes=60)  # A window whose reminder is not due yet
        statements: list[str] = []
        real = maintenance.connect

        def watched():
            db = real()
            db.set_trace_callback(statements.append)
            return db

        with mock.patch.object(maintenance, "connect", watched):
            self.assertEqual(maintenance.remind_due(), [])
        self.assertTrue(statements)
        writes = [item for item in statements if item.lstrip().upper().startswith(("BEGIN", "INSERT", "UPDATE", "DELETE", "REPLACE"))]
        self.assertEqual(writes, [])

    def test_an_idle_reminder_pass_does_not_wait_for_another_writer(self):
        import sqlite3
        import time
        from app.store import database_path

        self.open(self.admin, camera_ids=[self.d1], minutes=60)
        other = sqlite3.connect(database_path(), timeout=5, isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            self.assertEqual(maintenance.remind_due(), [])
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            other.execute("ROLLBACK")

    def test_the_list_says_when_a_window_is_ending_soon(self):
        window = self.open(self.admin, camera_ids=[self.d1], minutes=8).json()  # Reminder time two minutes before the end
        with mock.patch.object(maintenance, "_now", return_value=self.ends(window["id"]) - timedelta(minutes=1)):
            [shown] = self.admin.get("/api/maintenance").json()["windows"]
        self.assertTrue(shown["ending_soon"])


class AiPauseCase(MaintenanceCase):
    def setUp(self):
        super().setUp()
        with connect() as db:
            for camera_id in (self.d1, self.d2):
                db.execute("INSERT INTO ai_camera_settings(camera_id,config_override,enabled,use_substream) VALUES(?,1,1,0) "
                           "ON CONFLICT(camera_id) DO UPDATE SET config_override=1,enabled=1,use_substream=0", (camera_id,))
            db.execute("UPDATE ai_runtime_settings SET capacity_mode='fixed',max_active_cameras=4 WHERE singleton_id=1")
            db.execute("DELETE FROM ai_coverage")
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.manager = DetectionManager()
        self.manager.detector = object()  # Any model will do: the analysis below reads no frames
        self.started: list[str] = []

        async def analysis(camera_id, config, detector):
            self.started.append(camera_id)
            await self.manager._db(detection.set_camera_state, camera_id, "analyzed")
            await asyncio.Event().wait()  # Analysing until it is stopped

        self.manager._run = analysis
        patcher = mock.patch.object(self.manager, "_write_status")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.loop.run_until_complete(self.manager.stop()))

    def settle(self) -> None:
        """One pass of the AI worker's 5-second check, and a moment for the cameras it started to begin."""
        self.loop.run_until_complete(self.manager.reconcile())
        self.loop.run_until_complete(asyncio.sleep(0.05))

    def running(self) -> set[str]:
        return {camera_id for camera_id, task in self.manager.tasks.items() if not task.done()}

    def status(self, camera_id: str):
        with connect() as db:
            return db.execute("SELECT status,last_error,updated_at FROM ai_camera_settings WHERE camera_id=?", (camera_id,)).fetchone()

    def stretches(self, camera_id: str) -> list[tuple[str, str]]:
        with connect() as db:
            return [(row["state"], row["detail"]) for row in db.execute("SELECT state,detail FROM ai_coverage WHERE camera_id=? ORDER BY id", (camera_id,))]


class AiPauseTests(AiPauseCase):
    def test_a_camera_in_a_window_is_not_analysed_until_the_window_ends_and_other_cameras_carry_on(self):
        self.settle()
        self.assertEqual(self.running(), {self.d1, self.d2})
        window = self.open(self.admin, camera_ids=[self.d1], name="Lens clean").json()
        self.settle()
        self.assertEqual(self.running(), {self.d2})  # Its frames are no longer read at all
        self.assertEqual(tuple(self.status(self.d1))[:2], ("maintenance", "Paused for maintenance: Lens clean"))
        self.assertEqual(self.status(self.d2)["status"], "analyzed")
        self.settle()
        self.assertEqual(self.running(), {self.d2})  # Still paused on the next pass
        self.assertEqual(self.admin.post(f"/api/maintenance/{window['id']}/end").status_code, 200)
        self.settle()
        self.assertEqual(self.running(), {self.d1, self.d2})  # Resumed on the first pass after the window
        self.assertEqual(self.status(self.d1)["status"], "analyzed")
        self.assertEqual(self.started.count(self.d1), 2)
        self.assertEqual(self.started.count(self.d2), 1)  # Never interrupted

    def test_the_pause_is_recorded_as_not_analysed_with_the_window_as_its_reason(self):
        start = NOW() - timedelta(seconds=1)
        self.settle()
        window = self.open(self.admin, camera_ids=[self.d1], name="Lens clean").json()
        self.settle()
        self.assertEqual(self.admin.post(f"/api/maintenance/{window['id']}/end").status_code, 200)
        self.settle()
        stretches = self.stretches(self.d1)
        self.assertIn(("skipped", "Paused for maintenance: Lens clean"), stretches)
        self.assertEqual(stretches[-1][0], "analyzed")
        self.assertNotIn("skipped", [state for state, _ in self.stretches(self.d2)])
        with connect() as db:
            report = search.coverage(db, {self.d1: "Dock 1", self.d2: "Dock 2"}, start, NOW() + timedelta(seconds=1))
        by_camera = {camera["camera_id"]: camera for camera in report["cameras"]}
        paused = [item for item in by_camera[self.d1]["intervals"] if item["state"] == "skipped"]
        self.assertEqual([item["detail"] for item in paused], ["Paused for maintenance: Lens clean"])
        self.assertFalse(report["fully_analysed"])
        self.assertEqual([item for item in by_camera.get(self.d2, {"intervals": []})["intervals"] if item["state"] == "skipped"], [])

    def test_a_site_window_pauses_every_camera_and_a_window_on_another_camera_none(self):
        self.settle()
        self.open(self.admin, camera_ids=[self.y1], name="Yard")  # Yard 1 has no AI: nothing to pause, and the docks carry on
        self.settle()
        self.assertEqual(self.running(), {self.d1, self.d2})
        self.assertEqual(self.status(self.y1)["status"], "disabled")  # Not "maintenance": there was nothing to pause
        self.open(self.admin, scope="site", name="Switch swap")
        self.settle()
        self.assertEqual(self.running(), set())
        self.assertEqual({self.status(camera)["last_error"] for camera in (self.d1, self.d2)}, {"Paused for maintenance: Switch swap"})

    def test_a_camera_settled_by_a_message_during_a_window_stays_paused(self):
        self.open(self.admin, camera_ids=[self.d1], name="Lens clean")
        self.loop.run_until_complete(self.manager.sync_camera(self.d1, force_restart=True))  # What saving its AI settings does
        self.assertEqual(self.running(), set())
        self.assertEqual(self.status(self.d1)["status"], "maintenance")

    def test_a_paused_camera_takes_no_place_from_another(self):
        with connect() as db:
            db.execute("UPDATE ai_runtime_settings SET max_active_cameras=1 WHERE singleton_id=1")
        self.open(self.admin, camera_ids=[self.d1], name="Lens clean")  # Dock 1 comes first by name
        self.settle()
        self.assertEqual(self.running(), {self.d2})

    def test_a_pass_over_a_paused_camera_rewrites_nothing(self):
        self.open(self.admin, camera_ids=[self.d1], name="Lens clean")
        self.settle()
        before, stretches = self.status(self.d1)["updated_at"], self.stretches(self.d1)
        self.settle()
        self.settle()
        self.assertEqual((self.status(self.d1)["updated_at"], self.stretches(self.d1)), (before, stretches))

    def test_paused_cameras_match_what_each_camera_is_covered_by(self):
        self.open(self.admin, camera_ids=[self.d1], name="First", minutes=30)
        self.open(self.admin, scope="groups", group_ids=[self.dock], name="Dock work", minutes=60)
        with connect() as db:
            paused = maintenance.paused_cameras(db)
            self.assertEqual(paused, {camera: maintenance.covering(db, camera)["name"] for camera in (self.d1, self.d2)})
            self.assertEqual(paused[self.d1], "Dock work")  # The window that ends last, as covering() chooses
            self.assertEqual(maintenance.paused_cameras(db, NOW() + timedelta(hours=2)), {})


class LoopThreadTests(AiPauseCase):
    def test_the_pause_is_read_off_the_event_loop(self):
        loop_thread: list[int] = []
        on_loop: list[str] = []
        real = maintenance.paused_cameras

        def watched(*args, **kwargs):
            if loop_thread and threading.get_ident() == loop_thread[0]:
                on_loop.append("paused_cameras ran on the event loop")
            return real(*args, **kwargs)

        async def run():
            loop_thread.append(threading.get_ident())
            await self.manager.reconcile()

        self.open(self.admin, camera_ids=[self.d1], name="Lens clean")
        with mock.patch.object(maintenance, "paused_cameras", watched):
            self.loop.run_until_complete(run())
        self.assertEqual(on_loop, [])
