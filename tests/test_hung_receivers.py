"""One receiver that never answers cannot hold up every rule action, the NVR's own emails or the heartbeat (#527)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from app import email_outbox, heartbeat, rule_actions
from app.credentials import encrypt
from app.store import connect, initialize


class Receivers:
    """Stand-in receivers on 127.0.0.1: one that reads the request and never answers, one that answers 200 with an endless body."""

    async def start(self) -> None:
        self.hung_requests = 0

        async def hung(reader, writer):
            self.hung_requests += 1
            await reader.read(65536)
            await asyncio.sleep(3600)

        async def endless(reader, writer):
            await reader.read(65536)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n")
            chunk = b"x" * 65536
            try:
                while True:  # Far more than anything should keep: a gigabyte every few seconds, for ever
                    writer.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    await writer.drain()
            except (ConnectionError, OSError):
                pass

        self.servers = [await asyncio.start_server(hung, "127.0.0.1", 0), await asyncio.start_server(endless, "127.0.0.1", 0)]
        self.hung_url = f"http://127.0.0.1:{self.servers[0].sockets[0].getsockname()[1]}/hook"
        self.endless_url = f"http://127.0.0.1:{self.servers[1].sockets[0].getsockname()[1]}/hook"

    async def stop(self) -> None:
        for server in self.servers:
            server.close()


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        environment = mock.patch.dict(os.environ, {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_CREDENTIAL_KEY_PATH": str(root / "key")})
        environment.start()
        self.addCleanup(environment.stop)
        initialize()
        self.receivers = Receivers()
        await self.receivers.start()
        self.addAsyncCleanup(self.receivers.stop)
        for patch in (mock.patch.object(rule_actions, "_checked", lambda url, allow_private: url),  # The address check is not what is tested
                      mock.patch.object(heartbeat, "validate_target_url", lambda url, allow_private: url),
                      mock.patch.object(rule_actions, "ATTEMPT_DEADLINE_SECONDS", 1.0)):
            patch.start()
            self.addCleanup(patch.stop)
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode,device_service_url) VALUES('c1','Gate',X'01','disabled','http://192.0.2.10/onvif/device_service')")

    def action(self, kind: str, *, url: str | None = None, settings: dict | None = None) -> str:
        """A rule with one action of this kind, and a delivery of it due now."""
        rule_id, action_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = rule_actions._now().isoformat()
        secret = encrypt(json.dumps({"url": url, "signing_secret": "s" * 32})) if url else None
        with connect() as db:
            db.execute("INSERT INTO rules(id,name,condition_type,condition_json,classes_json,active_from) VALUES(?,?,'zone_entry','{}','[]',?)", (rule_id, f"Rule {rule_id[:4]}", now))
            db.execute("INSERT INTO rule_actions(id,rule_id,position,type,name,settings_json,secret_ciphertext) VALUES(?,?,0,?,?,?,?)",
                       (action_id, rule_id, kind, f"{kind} action", json.dumps({"allow_private": True, **(settings or {})}), secret))
        self.deliver(rule_id, action_id, kind)
        return action_id

    def deliver(self, rule_id: str, action_id: str, kind: str, status: str = "queued") -> str:
        delivery_id, now = str(uuid.uuid4()), rule_actions._now().isoformat()
        payload = {"event_id": str(uuid.uuid4()), "rule_name": "Rule", "label": "person", "camera_name": "Gate", "occurred_at": now}
        with connect() as db:
            db.execute("INSERT INTO rule_action_deliveries(id,rule_id,action_id,action_name,action_type,rule_event_id,payload_json,status,next_attempt_at,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (delivery_id, rule_id, action_id, f"{kind} action", kind, str(uuid.uuid4()), json.dumps(payload), status, now, now, now))
        return delivery_id

    def outcome(self, action_id: str) -> list[tuple]:
        with connect() as db:
            return [tuple(row) for row in db.execute("SELECT status,last_error FROM rule_action_deliveries WHERE action_id=?", (action_id,))]


class WorkerTests(Case):
    async def test_a_relay_on_another_rule_runs_while_a_receiver_hangs(self):
        hung = self.action("webhook", url=self.receivers.hung_url)
        with connect() as db:
            owner = db.execute("SELECT rule_id FROM rule_actions WHERE id=?", (hung,)).fetchone()[0]
        for _ in range(rule_actions.WORKERS):
            self.deliver(owner, hung, "webhook")
        relay = self.action("relay", settings={"camera_id": "c1", "duration_seconds": 0})
        pulsed = asyncio.Event()

        async def pulse(*args, **kwargs):
            pulsed.set()
            return "relay-1"

        manager = rule_actions.RuleActionManager(poll_seconds=0.05)
        with mock.patch("app.onvif_relay.pulse", pulse), mock.patch.object(email_outbox, "deliver_one", lambda: False):
            await manager.start()
            try:
                started = time.monotonic()
                await asyncio.wait_for(pulsed.wait(), timeout=5)
                self.assertLess(time.monotonic() - started, 3)
                await asyncio.sleep(0.3)
                self.assertEqual(self.receivers.hung_requests, 1)  # One delivery at a time for that receiver, not four
            finally:
                await manager.stop()
        self.assertEqual(self.outcome(relay)[0][0], "delivered")

    async def test_an_incident_email_goes_while_rule_actions_are_backed_up(self):
        for _ in range(rule_actions.WORKERS * 2):  # More hung receivers than workers: every rule-action worker is held
            self.action("webhook", url=self.receivers.hung_url)
        sent = asyncio.Event()
        loop = asyncio.get_running_loop()

        def deliver_one() -> bool:
            loop.call_soon_threadsafe(sent.set)
            return False

        manager = rule_actions.RuleActionManager(poll_seconds=0.05)
        with mock.patch.object(email_outbox, "deliver_one", deliver_one):
            await manager.start()
            try:
                await asyncio.wait_for(sent.wait(), timeout=3)
            finally:
                await manager.stop()

    async def test_a_receiver_that_never_answers_is_given_up_at_the_deadline_and_tried_again(self):
        action = self.action("webhook", url=self.receivers.hung_url)
        started = time.monotonic()
        self.assertTrue(await rule_actions.deliver_one())
        self.assertLess(time.monotonic() - started, 3)
        status, error = self.outcome(action)[0]
        self.assertEqual(status, "queued")  # A hang is a passing fault: tried again later
        self.assertIn("Timeout", error)

    async def test_an_endless_answer_is_never_read(self):
        started = time.monotonic()
        status = await rule_actions._post(self.receivers.endless_url, b"{}", {"Content-Type": "application/json"}, True)
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 2)  # The status, not a gigabyte later


class HeartbeatTests(Case):
    async def test_the_heartbeat_carries_on_after_a_receiver_hangs(self):
        results: list[dict] = []
        config = {"target_url_ciphertext": encrypt(self.receivers.hung_url), "signing_secret_ciphertext": encrypt("s" * 32), "allow_private": True,
                  "instance_id": "nvr", "test": False}
        with mock.patch.object(heartbeat, "record_heartbeat_result", lambda **kwargs: results.append(kwargs)), \
             mock.patch.object(heartbeat, "REQUEST_TIMEOUT_SECONDS", 0.5):
            started = time.monotonic()
            await asyncio.wait_for(heartbeat.HeartbeatManager()._send(config), timeout=5)
            self.assertLess(time.monotonic() - started, 3)
            await heartbeat.HeartbeatManager()._send({**config, "target_url_ciphertext": encrypt(self.receivers.endless_url)})
        self.assertFalse(results[0]["success"])
        self.assertEqual((results[1]["success"], results[1]["http_status"]), (True, 200))


class RestartTests(Case):
    async def test_a_relay_pulse_cut_off_by_a_restart_is_switched_off_not_pulsed_again(self):
        relay = self.action("relay", settings={"camera_id": "c1", "duration_seconds": 5})
        webhook = self.action("webhook", url=self.receivers.endless_url)
        with connect() as db:
            db.execute("UPDATE rule_action_deliveries SET status='sending'")  # Both were in flight when the process was killed
        calls: list[str] = []

        async def pulse(*args, **kwargs):
            calls.append("pulse")
            return "relay-1"

        async def release(*args, **kwargs):
            calls.append("release")
            return "relay-1"

        with mock.patch("app.onvif_relay.pulse", pulse), mock.patch("app.onvif_relay.release", release):
            interrupted = rule_actions.recover_inflight()
            self.assertEqual([item["id"] for item in interrupted], [row for row in self._ids(relay)])
            await rule_actions.release_interrupted(interrupted)
            while await rule_actions.deliver_one():
                pass
        self.assertEqual(calls, ["release"])
        self.assertEqual(self.outcome(relay), [("failed", "interrupted_by_restart_released")])
        self.assertEqual(self.outcome(webhook)[0][0], "delivered")  # Anything else is sent again as before

    def _ids(self, action_id: str) -> list[str]:
        with connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM rule_action_deliveries WHERE action_id=?", (action_id,))]


if __name__ == "__main__":
    unittest.main()
