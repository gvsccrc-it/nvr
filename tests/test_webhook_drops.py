"""A webhook receiver that falls behind no longer fills the database with a row per dropped event (#526).

As decided on the ticket: each receiver counts its dropped events (how many, the first and the latest), shown beside it with a button to clear;
an incident while it has dropped events in the last hour; events over an hour old are dropped and counted, not delivered late.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import webhooks
from app.credentials import encrypt
from app.store import connect, initialize
from tests.test_access_grants import GrantCase
from tests.test_support import as_given


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        environment = mock.patch.dict(os.environ, {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_CREDENTIAL_KEY_PATH": str(root / "key")})
        environment.start()
        self.addCleanup(environment.stop)
        initialize()

    def endpoint(self, url: str = "https://hooks.example.test/in") -> str:
        endpoint_id = str(uuid.uuid4())
        now = webhooks._now()
        with connect() as db:
            db.execute("INSERT INTO webhook_endpoints(id,name,target_url_ciphertext,signing_secret_ciphertext,events_json,allow_private,enabled,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,1,1,?,?)", (endpoint_id, "Control room", encrypt(url), encrypt("s" * 32), json.dumps(["ai.detection", "recording.failure"]), now, now))
        return endpoint_id

    def waiting(self, endpoint_id: str, count: int, *, age: timedelta = timedelta(0), status: str = "queued") -> None:
        at = (datetime.now(timezone.utc) - age).isoformat()
        with connect() as db:
            db.executemany("INSERT INTO webhook_deliveries(id,endpoint_id,endpoint_name,event_id,event_type,payload_json,status,attempt_count,next_attempt_at,created_at,updated_at) "
                           "VALUES(?,?,'Control room',?,'ai.detection','{}',?,0,?,?,?)",
                           [(str(uuid.uuid4()), endpoint_id, str(uuid.uuid4()), status, at, at, at) for _ in range(count)])

    def rows(self) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0]

    def dropped(self, endpoint_id: str) -> tuple:
        with connect() as db:
            return tuple(db.execute("SELECT dropped_count,dropped_first_at IS NOT NULL,dropped_last_at IS NOT NULL FROM webhook_endpoints WHERE id=?", (endpoint_id,)).fetchone())


class DropTests(Case):
    def test_a_receiver_at_capacity_adds_no_rows_for_ten_thousand_more_events(self):
        endpoint = self.endpoint()
        self.waiting(endpoint, webhooks.MAX_PENDING_PER_ENDPOINT)
        with connect() as db:
            for number in range(10_000):
                webhooks.enqueue_event(db, "ai.detection", {"event_id": f"e{number}", "event_type": "ai.detection"})
        self.assertEqual(self.rows(), webhooks.MAX_PENDING_PER_ENDPOINT)  # Was 11,000, each with its payload
        self.assertEqual(self.dropped(endpoint), (10_000, 1, 1))

    def test_a_receiver_with_room_still_gets_its_events(self):
        endpoint = self.endpoint()
        with connect() as db:
            self.assertEqual(webhooks.enqueue_event(db, "ai.detection", {"event_id": "e1", "event_type": "ai.detection"}), 1)
        self.assertEqual((self.rows(), self.dropped(endpoint)[0]), (1, 0))

    def test_events_waiting_over_an_hour_are_dropped_and_counted_not_delivered_late(self):
        endpoint = self.endpoint()
        self.waiting(endpoint, 1_500, age=timedelta(hours=2))
        self.waiting(endpoint, 10, age=timedelta(minutes=5))
        self.assertEqual(webhooks.drop_stale(), 1_500)
        self.assertEqual(self.rows(), 10)
        self.assertEqual(self.dropped(endpoint)[0], 1_500)
        statements: list[str] = []
        real = webhooks.connect

        def traced():
            db = real()
            db.set_trace_callback(statements.append)
            return db

        with mock.patch.object(webhooks, "connect", traced):
            self.assertEqual(webhooks.drop_stale(), 0)
        self.assertFalse([statement for statement in statements if statement.startswith(("BEGIN", "DELETE", "UPDATE"))])  # Nothing due: no write lock

    def test_the_history_is_pruned_in_batches(self):
        endpoint = self.endpoint()
        self.waiting(endpoint, 2_500, age=timedelta(days=40), status="delivered")
        self.waiting(endpoint, 5, age=timedelta(days=1), status="delivered")
        statements: list[str] = []
        real = webhooks.connect

        def traced():
            db = real()
            db.set_trace_callback(statements.append)
            return db

        with mock.patch.object(webhooks, "connect", traced):
            self.assertEqual(webhooks.prune_deliveries(), 2_500)
        self.assertEqual(len([statement for statement in statements if statement.startswith("DELETE")]), 3)  # 1,000 at a time
        self.assertEqual(self.rows(), 5)


class IncidentTests(Case):
    def incident(self, endpoint: str):
        with connect() as db:
            return db.execute("SELECT state,fault_type,summary FROM incidents WHERE incident_key=?", (f"webhook_drops:{endpoint}",)).fetchone()

    def test_an_incident_while_a_receiver_drops_events_cleared_an_hour_after_the_last(self):
        endpoint = self.endpoint()
        with connect() as db:
            webhooks._count_dropped(db, endpoint, 1_204, webhooks._now())
        webhooks.sync_drop_incidents()
        state, fault, summary = self.incident(endpoint)
        self.assertEqual((state, fault), ("active", "webhook_drops"))
        self.assertIn("1204 events dropped", summary)
        webhooks.sync_drop_incidents(datetime.now(timezone.utc) + timedelta(hours=1, minutes=1))
        self.assertEqual(self.incident(endpoint)[0], "recovered")

    def test_a_drop_incident_is_a_system_fault(self):
        from app import incidents

        self.assertIn("webhook_drops", incidents.SYSTEM_FAULT_TYPES)  # Its own webhook is system.failure, not a recording failure


class ClearTests(GrantCase):
    def test_an_administrator_sees_the_count_and_clears_it_and_an_operator_cannot(self):
        endpoint = str(uuid.uuid4())
        now = webhooks._now()
        with connect() as db:
            db.execute("INSERT INTO webhook_endpoints(id,name,target_url_ciphertext,signing_secret_ciphertext,events_json,allow_private,enabled,created_at,updated_at) "
                       "VALUES(?,'Control room',?,?,'[\"ai.detection\"]',0,1,?,?)", (endpoint, encrypt("https://hooks.example.test/in"), encrypt("s" * 32), now, now))
            webhooks._count_dropped(db, endpoint, 42, now)
        listed = next(hook for hook in self.admin.get("/api/webhooks").json()["webhooks"] if hook["id"] == endpoint)
        self.assertEqual(listed["dropped"]["count"], 42)
        self.assertEqual(self.limited.post(f"/api/webhooks/{endpoint}/dropped/clear").status_code, 403)
        self.assertEqual(self.admin.post(f"/api/webhooks/{endpoint}/dropped/clear").status_code, 204)
        listed = next(hook for hook in self.admin.get("/api/webhooks").json()["webhooks"] if hook["id"] == endpoint)
        self.assertEqual(listed["dropped"], {"count": 0, "first_at": None, "last_at": None})
        self.assertEqual([entry["resource_id"] for entry in self.audit("webhook_dropped_cleared")], [endpoint])
        self.assertEqual(self.admin.post("/api/webhooks/nothing/dropped/clear").status_code, 404)


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        environment = mock.patch.dict(os.environ, {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_CREDENTIAL_KEY_PATH": str(root / "key")})
        environment.start()
        self.addCleanup(environment.stop)
        initialize()
        self.connections = 0
        self.answer = b"ok"

        async def receive(reader, writer):
            self.connections += 1
            try:
                while True:  # Requests on one connection, one after another
                    head = await reader.readuntil(b"\r\n\r\n")
                    length = int(next((line.split(b":")[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")), b"0"))
                    await reader.readexactly(length)
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(self.answer), self.answer))
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass

        self.server = await asyncio.start_server(receive, "127.0.0.1", 0)
        self.addAsyncCleanup(self._stop)
        url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/in"
        patcher = mock.patch.object(webhooks, "pin_target_url", as_given)  # Loopback, for the test (delivery pins the checked address, #531)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.endpoint = str(uuid.uuid4())
        now = webhooks._now()
        with connect() as db:
            db.execute("INSERT INTO webhook_endpoints(id,name,target_url_ciphertext,signing_secret_ciphertext,events_json,allow_private,enabled,created_at,updated_at) "
                       "VALUES(?,'Control room',?,?,'[\"ai.detection\"]',1,1,?,?)", (self.endpoint, encrypt(url), encrypt("s" * 32), now, now))
        self.manager = webhooks.WebhookManager()
        self.addAsyncCleanup(self.manager.stop)

    async def _stop(self):
        self.server.close()

    async def deliver(self, count: int) -> list[str]:
        with connect() as db:
            for _ in range(count):
                webhooks.enqueue_event(db, "ai.detection", {"event_id": str(uuid.uuid4()), "event_type": "ai.detection"})
        while (delivery := await asyncio.to_thread(webhooks.claim_due_delivery)) is not None:
            await self.manager._deliver(delivery)
        with connect() as db:
            return [row[0] for row in db.execute("SELECT status FROM webhook_deliveries")]

    async def test_one_receivers_deliveries_reuse_a_connection(self):
        self.assertEqual(await self.deliver(5), ["delivered"] * 5)
        self.assertEqual(self.connections, 1)

    async def test_a_long_answer_is_cut_off_and_its_connection_not_reused(self):
        self.answer = b"x" * (webhooks.ANSWER_BYTES * 4)
        self.assertEqual(await self.deliver(2), ["delivered"] * 2)  # The status was 200: delivered all the same
        self.assertEqual(self.connections, 2)


if __name__ == "__main__":
    unittest.main()
