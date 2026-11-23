"""A camera's signed footage chain that no longer matches its signature (#507).

Each minute the chain's new links are checked from the last signature. A changed link was only logged, every minute for good, and nothing
recorded on the camera afterwards was ever signed again. The owner's decision on the ticket: the break is recorded, signed and permanent,
signing resumes from it at once, an incident stays active until an administrator acknowledges it from a browser session with a note, and
any verification or export that spans the break says so."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import footage_chain, self_test
from app.store import connect
from tests import test_footage_chain as base  # The module, not its class: a class imported here would run its tests again
from tests.scene_case import SceneTestCase

START = base.START


class BreakCase(SceneTestCase):
    add_segment = base.FootageChainTests.add_segment
    chain = base.FootageChainTests.chain
    status_of = base.FootageChainTests.status_of

    def setUp(self):
        super().setUp()
        footage_chain._unreadable.clear()
        self.root = Path(self.temporary.name) / "recordings"
        (self.root / self.camera_id).mkdir()
        self.segments = [self.add_segment(index) for index in range(4)]

    def verify(self, start=None, end=None) -> dict:
        response = self.client.get(f"/api/cameras/{self.camera_id}/footage/verify",
                                   params={"start": (start or START - timedelta(minutes=1)).isoformat(), "end": (end or START + timedelta(minutes=1)).isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def change_link(self, seq: int, *, rehash: bool = True) -> None:
        """Change what link ``seq`` says a segment's hash was (the signing pass checks from the last signed link on, so the tests change that
        link or a later one): rewritten with its own hash recomputed (as someone editing the database would),
        or left with the old one (as damage would)."""
        with connect() as db:
            link = dict(db.execute("SELECT * FROM footage_chain WHERE camera_id=? AND seq=?", (self.camera_id, seq)).fetchone())
            link["sha256"] = "0" * 64
            db.execute("UPDATE footage_chain SET sha256=?,link_hash=? WHERE id=?", (link["sha256"], footage_chain.link_hash(link) if rehash else link["link_hash"], link["id"]))

    def new_footage(self, index: int, seconds: int) -> int:
        segment = self.add_segment(index, START + timedelta(seconds=seconds))
        while asyncio.run(footage_chain.append_pending()):
            pass
        return segment

    def breaks(self) -> list[sqlite3.Row]:
        with connect() as db:
            return db.execute("SELECT * FROM footage_chain_breaks ORDER BY detected_at").fetchall()

    def incidents(self) -> list[sqlite3.Row]:
        with connect() as db:
            return db.execute("SELECT * FROM incidents WHERE fault_type='footage_chain' ORDER BY first_seen_at").fetchall()


class BreakTests(BreakCase):
    def test_a_changed_link_raises_one_incident_naming_the_camera_and_the_link_at_the_next_signing(self):
        self.chain()  # Links 1-4 signed
        self.change_link(4)
        self.new_footage(8, 20)
        sent = []
        with patch("app.webhooks.enqueue_event", lambda db, kind, payload: sent.append((kind, payload))), self.assertLogs("app.footage_chain", "ERROR"):
            self.assertEqual(footage_chain.sign_heads(), 1)  # Signing carries on
        (record,) = self.breaks()
        self.assertEqual((record["camera_id"], record["broken_seq"], record["signed_seq"], record["resumed_seq"]), (self.camera_id, 4, 4, 5))
        self.assertTrue(footage_chain.verify_break(dict(record)))
        (incident,) = self.incidents()
        self.assertEqual((incident["camera_id"], incident["state"], incident["fault_active"]), (self.camera_id, "active", 1))
        self.assertIn("link 4", incident["summary"])
        self.assertEqual([kind for kind, _ in sent], ["system.failure"])  # The incident's webhook

    def test_the_break_is_logged_once_and_later_footage_is_signed_again(self):
        self.chain()
        self.change_link(4)
        self.new_footage(8, 20)
        with self.assertLogs("app.footage_chain", "ERROR") as logged:
            footage_chain.sign_heads()
            for index, seconds in ((9, 22), (10, 24)):
                self.new_footage(index, seconds)
                footage_chain.sign_heads()
        self.assertEqual(len(logged.output), 1)
        self.assertEqual(len(self.breaks()), 1)
        self.assertEqual(len(self.incidents()), 1)
        with connect() as db:
            self.assertEqual(db.execute("SELECT MAX(upto_seq) FROM footage_chain_signatures WHERE camera_id=?", (self.camera_id,)).fetchone()[0], 7)

    def test_footage_after_the_break_verifies_and_a_range_across_it_says_so(self):
        self.chain()
        self.change_link(4)
        later = self.new_footage(8, 40)
        with self.assertLogs("app.footage_chain", "ERROR"):
            footage_chain.sign_heads()
        self.new_footage(9, 42)
        footage_chain.sign_heads()
        after = self.verify(START + timedelta(seconds=39), START + timedelta(seconds=50))
        self.assertTrue(after["valid"], after)
        self.assertEqual(after["breaks"], [])
        across = self.verify()
        self.assertFalse(across["valid"])
        self.assertIn("broken at link 4", across["chain_error"])
        self.assertEqual([(item["broken_seq"], item["resumed_seq"], item["signature_valid"]) for item in across["breaks"]], [(4, 5, True)])
        self.assertEqual(self.status_of(after)[later], "ok")

    def test_a_head_that_no_longer_hashes_is_where_signing_resumes_and_what_follows_still_verifies(self):
        self.chain()
        self.change_link(4, rehash=False)  # The signed head itself is damaged: its stored hash no longer matches what it says
        self.new_footage(8, 40)
        with self.assertLogs("app.footage_chain", "ERROR"):
            footage_chain.sign_heads()
        (record,) = self.breaks()
        self.assertEqual(record["broken_seq"], 4)
        later = [self.new_footage(9, 42), self.new_footage(10, 44)]
        self.assertEqual(footage_chain.sign_heads(), 1)  # No second break: checked from where signing resumed, as signed
        self.assertEqual(len(self.breaks()), 1)
        result = self.verify(START + timedelta(seconds=41), START + timedelta(seconds=50))
        self.assertTrue(result["valid"], result)
        self.assertEqual({self.status_of(result)[segment] for segment in later}, {"ok"})

    def test_a_second_break_is_a_record_and_an_incident_of_its_own(self):
        self.chain()
        self.change_link(4)
        self.new_footage(8, 20)
        with self.assertLogs("app.footage_chain", "ERROR"):
            footage_chain.sign_heads()
        self.new_footage(9, 22)
        footage_chain.sign_heads()
        self.change_link(6)
        self.new_footage(10, 24)
        with self.assertLogs("app.footage_chain", "ERROR"):
            footage_chain.sign_heads()
        self.assertEqual([(row["broken_seq"], row["resumed_seq"]) for row in self.breaks()], [(4, 5), (6, 7)])
        self.assertEqual(len({row["incident_key"] for row in self.incidents()}), 2)


class RecordTests(BreakCase):
    def broken(self) -> sqlite3.Row:
        self.chain()
        self.change_link(4)
        self.new_footage(8, 20)
        with self.assertLogs("app.footage_chain", "ERROR"):
            footage_chain.sign_heads()
        return self.breaks()[0]

    def test_the_record_cannot_be_deleted_or_changed(self):
        record = self.broken()
        with connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM footage_chain_breaks WHERE id=?", (record["id"],))
            for change in ("broken_seq=1", "reason='nothing happened'", "detected_at='2020-01-01'", "signature='x'"):
                with self.assertRaises(sqlite3.IntegrityError, msg=change):
                    db.execute(f"UPDATE footage_chain_breaks SET {change} WHERE id=?", (record["id"],))
        self.assertEqual(dict(self.breaks()[0]), dict(record))

    def test_an_administrator_acknowledges_it_once_with_a_note_and_its_incident_recovers(self):
        record = self.broken()
        refused = self.client.post(f"/api/footage/chain-breaks/{record['id']}/acknowledge", json={"note": "   "})
        self.assertEqual(refused.status_code, 422)
        answer = self.client.post(f"/api/footage/chain-breaks/{record['id']}/acknowledge", json={"note": "Restored a damaged database copy"})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["acknowledgement_note"], "Restored a damaged database copy")
        (incident,) = self.incidents()
        self.assertEqual((incident["state"], incident["fault_active"]), ("recovered", 0))
        with connect() as db:
            audit = db.execute("SELECT * FROM audit_log WHERE action='footage_chain_break_acknowledged'").fetchall()
            self.assertEqual(len(audit), 1)
            self.assertIn("Restored a damaged database copy", audit[0]["details_json"])
            with self.assertRaises(sqlite3.IntegrityError):  # Once
                db.execute("UPDATE footage_chain_breaks SET acknowledgement_note='changed' WHERE id=?", (record["id"],))
        again = self.client.post(f"/api/footage/chain-breaks/{record['id']}/acknowledge", json={"note": "Another note"})
        self.assertEqual(again.json()["acknowledgement_note"], "Restored a damaged database copy")
        listed = self.client.get("/api/footage/chain-breaks").json()["breaks"]
        self.assertEqual([(item["id"], item["signature_valid"], item["acknowledged_at"] is not None) for item in listed], [(record["id"], True, True)])
        self.assertEqual(self.client.post("/api/footage/chain-breaks/unknown/acknowledge", json={"note": "x"}).status_code, 404)

    def test_an_api_token_cannot_acknowledge_it(self):
        record = self.broken()
        created = self.client.post("/api/admin/api-tokens", json={"name": "automation", "scopes": ["admin"]})
        self.assertEqual(created.status_code, 201, created.text)
        answer = TestClient(self.context.app).post(f"/api/footage/chain-breaks/{record['id']}/acknowledge", json={"note": "by a script"},
                                           headers={"Authorization": f"Bearer {created.json()['secret']}"})
        self.assertEqual(answer.status_code, 403)
        self.assertIsNone(self.breaks()[0]["acknowledged_at"])
        self.assertEqual(self.incidents()[0]["fault_active"], 1)


class SelfTestAndExportTests(BreakCase):
    def test_the_nightly_self_test_checks_each_cameras_chain_since_its_last_signature(self):
        self.chain()
        intact = [item for item in self_test._check_footage_chains() if item["kind"] == "footage_chain"]
        self.assertEqual([(item["camera_id"], item["passed"]) for item in intact], [(self.camera_id, True)])
        self.change_link(4)  # Not yet noticed by the signing pass: the self-test finds it on its own
        (found,) = self_test._check_footage_chains()
        self.assertFalse(found["passed"])
        self.assertIn("link 4", found["detail"])
        self.new_footage(8, 20)
        with self.assertLogs("app.footage_chain", "ERROR"):
            footage_chain.sign_heads()
        (after,) = self_test._check_footage_chains()
        self.assertTrue(after["passed"], after)  # Signing resumed from the recorded break

    def test_an_export_extract_that_spans_a_break_says_so_and_one_after_it_verifies(self):
        self.chain()
        self.change_link(4)
        later = self.new_footage(8, 40)
        with self.assertLogs("app.footage_chain", "ERROR"):
            footage_chain.sign_heads()
        self.new_footage(9, 42)
        footage_chain.sign_heads()
        with connect() as db:
            sources = {row["id"]: row["relative_path"] for row in db.execute("SELECT id,relative_path FROM recording_segments")}
            across = footage_chain.extract(db, self.camera_id, [self.segments[2], later])
            after = footage_chain.extract(db, self.camera_id, [later])
        root = Path(self.temporary.name) / "recordings"

        def source(segment_id):
            return {"segment_id": segment_id, "relative_path": sources[segment_id], "sha256": footage_chain.file_sha256(root / sources[segment_id])}

        spanning = footage_chain.verify_extract(across, [source(self.segments[2]), source(later)])
        self.assertFalse(spanning["valid"])
        self.assertEqual([(item["broken_seq"], item["resumed_seq"]) for item in spanning["breaks"]], [(4, 5)])
        self.assertTrue(footage_chain.verify_extract(after, [source(later)])["valid"])
        forged = {**across, "breaks": [{**across["breaks"][0], "reason": "nothing to see"}]}
        self.assertIn("not validly signed", footage_chain.verify_extract(forged, [])["error"])
