"""Who may verify footage, how much at a time, and that a verification waits its turn, says where it stands and stops when left (#508).

Verifying re-reads every file of its range, and footage on a cold tier is downloaded first. The decision on the ticket: the export scope is
needed; an administrator may check up to a week, archived footage included; anyone else a day of footage on the hot disk, archived footage
being "not checked (archived)"; two at once and one for each person, the rest waiting in line with their place shown; each one audited."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient

from app import footage_chain
from app.api import footage_chain as verify_api
from app.recording import recordings_path
from app.store import connect
from tests.test_access_grants import START, GrantCase


class VerifyCase(GrantCase):
    def setUp(self):
        super().setUp()
        footage_chain._unreadable.clear()
        self.segments = [self.add_segment(self.d1, minute) for minute in range(3)]
        for segment in self.segments:
            with connect() as db:
                relative = db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (segment,)).fetchone()[0]
            path = recordings_path() / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"footage {segment}".encode() * 50)
        while asyncio.run(footage_chain.append_pending()):
            pass
        footage_chain.sign_heads()

    def params(self, hours: float = 3) -> dict:  # The fixture's footage starts two hours ago
        end = datetime.now(timezone.utc)
        return {"start": (end - timedelta(hours=hours)).isoformat(), "end": end.isoformat()}

    def verify(self, client, hours: float = 3, camera: str | None = None):
        return client.get(f"/api/cameras/{camera or self.d1}/footage/verify", params=self.params(hours))

    def token(self, scopes: list[str]) -> dict:
        created = self.admin.post("/api/admin/api-tokens", json={"name": f"verifier-{'-'.join(scopes)}", "scopes": scopes})
        self.assertEqual(created.status_code, 201, created.text)
        return {"Authorization": f"Bearer {created.json()['secret']}"}


class WhoAndHowMuchTests(VerifyCase):
    def test_a_read_only_token_cannot_verify_and_an_export_token_can(self):
        anonymous = TestClient(self.app)
        self.assertEqual(anonymous.get(f"/api/cameras/{self.d1}/footage/verify", params=self.params(), headers=self.token(["read"])).status_code, 403)
        allowed = anonymous.get(f"/api/cameras/{self.d1}/footage/verify", params=self.params(), headers=self.token(["read", "export"]))
        self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_an_administrator_may_check_a_week_and_anyone_else_a_day(self):
        self.assertEqual(self.verify(self.admin, hours=24 * 7).status_code, 200)
        self.assertEqual(self.verify(self.admin, hours=24 * 7 + 1).status_code, 422)
        self.assertEqual(self.verify(self.full, hours=24).status_code, 200)
        refused = self.verify(self.full, hours=25)
        self.assertEqual((refused.status_code, refused.json()["detail"]), (422, "Verify up to 24 hours at a time"))

    def test_a_camera_the_person_may_not_see_is_not_found(self):
        self.assertEqual(self.verify(self.limited, camera=self.y1).status_code, 404)
        self.assertEqual(self.verify(self.limited).status_code, 200)

    def test_archived_footage_is_not_read_for_someone_who_is_not_an_administrator(self):
        archived = self.segments[0]
        with connect() as db:
            relative = db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (archived,)).fetchone()[0]
            db.execute("UPDATE recording_segments SET tier_id='cold-1',tier_key=relative_path WHERE id=?", (archived,))
        (recordings_path() / relative).unlink()  # It has moved: reading it would download it from the tier
        fetched = []
        real = footage_chain.file_sha256

        def hashing(path):
            fetched.append(str(path))
            return real(path)

        with mock.patch("app.tiering.cold_path", side_effect=AssertionError("an archived file was fetched")), \
                mock.patch.object(footage_chain, "file_sha256", hashing):
            result = self.verify(self.full).json()
        statuses = {item["segment_id"]: item["status"] for item in result["segments"]}
        self.assertEqual(statuses[archived], "not_checked_archived")
        self.assertTrue(result["valid"], result)  # Not checked is not a problem
        self.assertNotIn(relative, " ".join(fetched))

    def test_every_verification_is_in_the_security_log(self):
        self.assertEqual(self.verify(self.full).status_code, 200)
        (entry,) = self.audit("footage_verified")
        self.assertEqual((entry["actor_id"], entry["resource_id"]), (self.full_id, self.d1))
        self.assertEqual((entry["details"]["outcome"], entry["details"]["segments"], entry["details"]["archived_checked"]), ("valid", 3, False))
        self.assertEqual(self.verify(self.admin).status_code, 200)
        self.assertTrue(self.audit("footage_verified")[1]["details"]["archived_checked"])


class TurnTests(VerifyCase):
    def test_two_run_at_once_one_for_each_person_and_the_rest_wait_in_order(self):
        async def scenario():
            turns = verify_api._Turns(2)
            first, second, third, fourth = (turns.ask(person) for person in (1, 2, 3, 4))
            started = [turn.done() for turn in (first, second, third, fourth)]
            places = [turns.position(turn) for turn in (first, second, third, fourth)]
            busy = turns.has(1), turns.has(3), turns.has(9)
            turns.leave(4, fourth)  # Gave up while waiting: its place goes, nobody starts
            after_giving_up = [turn.done() for turn in (third,)] + [turns.position(third)]
            turns.leave(1, first)  # Finished: the next in line starts
            return started, places, busy, after_giving_up, third.done(), turns.running

        started, places, busy, after_giving_up, third_started, running = asyncio.run(scenario())
        self.assertEqual(started, [True, True, False, False])
        self.assertEqual(places, [0, 0, 1, 2])
        self.assertEqual(busy, (True, True, False))
        self.assertEqual(after_giving_up, [False, 1])
        self.assertTrue(third_started)
        self.assertEqual(running, {2, 3})

    def test_a_second_verification_by_the_same_person_is_refused_while_one_is_under_way(self):
        async def hold():
            return verify_api._turns.ask(self.full_id)

        turn = asyncio.run(hold())
        try:
            refused = self.verify(self.full)
            self.assertEqual(refused.status_code, 429)
            self.assertIn("already have a footage verification", refused.json()["detail"])
            self.assertEqual(self.verify(self.admin).status_code, 200)  # Someone else still can
        finally:
            verify_api._turns.leave(self.full_id, turn)
        self.assertEqual(self.verify(self.full).status_code, 200)

    def test_the_stream_says_where_it_waits_how_far_it_has_got_and_then_the_result(self):
        others = []

        async def crowd():
            others.extend([verify_api._turns.ask(person) for person in (900, 901)])  # Two under way: this one waits

        asyncio.run(crowd())
        lines: list[dict] = []
        released = threading.Timer(1.5, lambda: verify_api._turns.leave(900, others[0]))
        released.start()
        try:
            with self.full.stream("POST", f"/api/cameras/{self.d1}/footage/verify/stream", json=self.params()) as response:
                self.assertEqual(response.status_code, 200)
                for line in response.iter_lines():
                    lines.append(json.loads(line))
        finally:
            released.join()
            verify_api._turns.leave(901, others[1])
        self.assertEqual(lines[0], {"queued": 1})
        self.assertTrue(any("progress" in line for line in lines), lines)
        self.assertTrue(lines[-1]["result"]["valid"])
        self.assertEqual(verify_api._turns.running, set())


class CancelTests(VerifyCase):
    def test_a_verification_left_by_its_requester_stops_between_files_gives_its_turn_back_and_is_logged_as_cancelled(self):
        started, carry_on = threading.Event(), threading.Event()
        real = footage_chain.file_sha256

        def slow(path):
            started.set()
            carry_on.wait(5)  # The requester leaves while the first file is being read
            return real(path)

        user = mock.MagicMock()
        user.__getitem__.side_effect = {"id": self.full_id, "role": "operator"}.__getitem__

        async def scenario():
            events = verify_api._verification(user, self.d1, START - timedelta(minutes=5), START + timedelta(minutes=10), False)
            seen = [await anext(events)]  # Its first progress: it has started
            while not started.is_set():
                await asyncio.sleep(0.01)
            await events.aclose()  # The requester went away
            carry_on.set()
            for _ in range(200):
                if verify_api.footage_chain and self.audit("footage_verified"):
                    break
                await asyncio.sleep(0.02)
            return seen, verify_api._turns.has(self.full_id)

        with mock.patch.object(footage_chain, "file_sha256", slow):
            seen, still_held = asyncio.run(scenario())
        self.assertIn("progress", seen[0])
        self.assertFalse(still_held)
        (entry,) = self.audit("footage_verified")
        self.assertEqual(entry["details"]["outcome"], "cancelled")
        self.assertLess(entry["details"]["segments"], 3)  # It stopped before reading every file
