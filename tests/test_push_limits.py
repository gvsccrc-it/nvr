"""A signed-in person cannot stall or crash the background worker through a push subscription (#523)."""
from __future__ import annotations

import gzip
import os
import resource
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

import app.migrations as migrations
from app import native_push, push
from app.migrations import m0080_push_services
from app.store import connect, initialize

_ENV = ("NVR_DB_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_PUSH_EXTRA_HOSTS")
KEYS = {"p256dh": "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8", "auth": "BTBZMqHH6r4Tts7J_aSIgg"}


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(Path(self.temporary.name) / "credentials.key")
        os.environ.pop("NVR_PUSH_EXTRA_HOSTS", None)
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'viewer','unused','operator')")
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(2,'other','unused','operator')")

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()


class AddressTests(Case):
    def test_only_the_browsers_push_services_are_accepted(self):
        for host in ("fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com", "api17.push.apple.com", "db5p.notify.windows.com", "FCM.googleapis.com"):
            self.assertTrue(push.push_service_host(host), host)
        for host in ("push.example.test", "googleapis.com", "fcm.googleapis.com.example.test", "push.apple.com.example.test", "notify.windows.com.evil.example.test", ""):
            self.assertFalse(push.push_service_host(host), host)

    def test_a_subscription_to_any_other_address_is_refused(self):
        with self.assertRaisesRegex(ValueError, "browser's own push service"):
            push.subscribe(1, "https://push.example.test/abc", KEYS["p256dh"], KEYS["auth"], "laptop")
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0], 0)

    def test_an_administrator_can_add_a_host_with_a_setting(self):
        os.environ["NVR_PUSH_EXTRA_HOSTS"] = "push.vendor.example.test, other.example.test"
        self.assertTrue(push.push_service_host("push.vendor.example.test"))
        self.assertFalse(push.push_service_host("vendor.example.test"))

    def test_the_upgrade_removes_subscriptions_to_other_addresses_and_keeps_the_rest(self):
        with connect() as db:
            for name, endpoint, kind in (("google", "https://fcm.googleapis.com/fcm/send/abc", "web"), ("apple", "https://web.push.apple.com/abc", "web"),
                                         ("elsewhere", "https://push.example.test/abc", "web"), ("plain", "http://fcm.googleapis.com/abc", "web"), ("phone", "fcm:token", "fcm")):
                db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh,auth,device_label,kind) VALUES(?,?,?,'k','a',?,?)", (name, 1, endpoint, name, kind))
            m0080_push_services.upgrade(db)
            self.assertEqual([row[0] for row in db.execute("SELECT id FROM push_subscriptions ORDER BY id")], ["apple", "google", "phone"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='push_unsubscribed'").fetchone()[0], 2)
        self.assertIn((80, m0080_push_services), migrations.MIGRATIONS)


class DeviceLimitTests(Case):
    def subscribe(self, user_id: int, name: str) -> None:
        with patch("app.push.validate_endpoint", side_effect=lambda endpoint: endpoint):
            push.subscribe(user_id, f"https://fcm.googleapis.com/fcm/send/{name}", KEYS["p256dh"], KEYS["auth"], name)

    def devices(self, user_id: int) -> list[str]:
        with connect() as db:
            return [row[0] for row in db.execute("SELECT device_label FROM push_subscriptions WHERE user_id=? ORDER BY device_label", (user_id,))]

    def test_an_eleventh_device_takes_the_place_of_the_one_used_longest_ago(self):
        for index in range(push.MAX_DEVICES):
            self.subscribe(1, f"device-{index:02d}")
        with connect() as db:
            db.execute("UPDATE push_subscriptions SET created_at='2026-01-01T00:00:00Z' WHERE device_label='device-03'")  # The oldest, and never used since
            db.execute("UPDATE push_subscriptions SET created_at='2025-01-01T00:00:00Z',last_success_at='2026-11-20T00:00:00Z' WHERE device_label='device-05'")  # Older, but used this week
        self.subscribe(2, "someone-elses")
        self.subscribe(1, "device-new")
        devices = self.devices(1)
        self.assertEqual(len(devices), push.MAX_DEVICES)
        self.assertNotIn("device-03", devices)
        self.assertIn("device-05", devices)
        self.assertIn("device-new", devices)
        self.assertEqual(self.devices(2), ["someone-elses"])

    def test_subscribing_a_known_device_again_removes_nothing(self):
        for index in range(push.MAX_DEVICES):
            self.subscribe(1, f"device-{index:02d}")
        self.subscribe(1, "device-00")
        self.assertEqual(len(self.devices(1)), push.MAX_DEVICES)

    def test_phones_count_with_browsers(self):
        for index in range(push.MAX_DEVICES):
            self.subscribe(1, f"device-{index:02d}")
        native_push.register(1, "android", "a" * 150, "phone")
        devices = self.devices(1)
        self.assertEqual(len(devices), push.MAX_DEVICES)
        self.assertIn("phone", devices)


class Hostile(BaseHTTPRequestHandler):
    mode = "bomb"
    served = 0

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("content-length", "0")))
        if Hostile.mode == "bomb":
            body = gzip.compress(bytes(60 * 1024 * 1024), compresslevel=1)  # A small answer on the wire, 60 MB once expanded
            self.send_response(201)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            Hostile.served = len(body)
            try:
                self.wfile.write(body)
            except OSError:
                pass
            return
        try:
            self.wfile.write(b"HTTP/1.1 201 Created\r\n")
            for _ in range(200):  # A byte of header now and then: no single read ever times out
                self.wfile.write(b"X")
                self.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass

    def log_message(self, *args):
        pass


class SendTests(Case):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Hostile)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/push"
        self.client = httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        self.addCleanup(self.client.close)

    def test_an_answer_that_expands_enormously_is_never_read(self):
        Hostile.mode = "bomb"
        expanded = 60 * 1024 * 1024
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        status = push._post_for_status(self.client, self.url, b"payload", {})
        grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 - before
        self.assertEqual(status, 201)
        self.assertGreater(Hostile.served, 0)
        self.assertLess(Hostile.served, expanded // 100, "small on the wire")
        self.assertLess(grown, expanded // 3, "the process did not grow by the expanded answer")

    def test_a_service_that_drips_its_answer_is_abandoned_at_the_deadline(self):
        Hostile.mode = "drip"
        started = time.monotonic()
        with patch.object(push, "SEND_DEADLINE_SECONDS", 0.5), self.assertRaises(httpx.ReadTimeout):
            push._post_for_status(self.client, self.url, b"payload", {})
        self.assertLess(time.monotonic() - started, 2.0)


class RetryLimitTests(Case):
    def test_a_delivery_left_sending_by_a_crash_is_failed_once_it_has_had_its_attempts(self):
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('cam','Cam',X'01')")
            db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh,auth) VALUES('sub',1,'https://fcm.googleapis.com/fcm/send/1','k','a')")
            for name, attempts in (("fresh", 1), ("worn", push.MAX_ATTEMPTS)):
                alarm = str(uuid.uuid4())
                db.execute("INSERT INTO alarms(id,rule_name,camera_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,state,created_at,updated_at) "
                           "VALUES(?,'Rule','cam','person','zone_entry','2026-11-22T00:00:00Z','[]','2026-11-22T00:00:00Z','2026-11-22T00:00:10Z','new','2026-11-22T00:00:00Z','2026-11-22T00:00:00Z')", (alarm,))
                db.execute("INSERT INTO push_deliveries(id,subscription_id,alarm_id,payload_json,status,attempt_count,next_attempt_at,created_at) VALUES(?,?,?,'{}','sending',?,?,?)",
                           (name, "sub", alarm, attempts, "2026-11-22T00:00:00Z", "2026-11-22T00:00:00Z"))
        push.recover_inflight()
        with connect() as db:
            states = dict(db.execute("SELECT id,status FROM push_deliveries").fetchall())
        self.assertEqual(states, {"fresh": "queued", "worn": "failed"})


if __name__ == "__main__":
    unittest.main()
