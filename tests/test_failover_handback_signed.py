"""The primary believes only what its standby signed during a handback, and no request between them can be sent twice (#502).

#343 made the primary obey only a signed lease answer; the rest of the handback still trusted whatever came back: the footage list, each
file (checked only against a digest in the same unsigned answer), a 404 (which made the primary skip that footage for good) and "handback
complete". Someone on the path between the two NVRs could plant footage, which the primary then indexed and signed as its own, drop real
footage, or leave the standby waiting. A captured request could be sent again for a minute, and clocks more than a minute apart were
reported as different secrets.

A stand-in standby answers the primary's real requests here, through httpx's mock transport, as the real one does or as someone in the
middle would."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from app import failover
from app.store import connect
from tests.test_failover import LINK, SECRET, FailoverCase

FOOTAGE = b"the standby's own recording " * 40
PLANTED = b"something else entirely " * 40


class Standby:
    """Answers the primary's handback requests. ``forge`` names what someone in the middle changes."""

    def __init__(self, listing: list[dict], forge: set[str] = frozenset(), missing: set[int] = frozenset()):
        self.listing, self.forge, self.missing = listing, set(forge), set(missing)
        self.completed: list[dict] = []

    def signed(self, request: httpx.Request, body: bytes, signed_body: bytes | None = None) -> dict[str, str]:
        target = request.url.raw_path.decode()
        return failover.sign_answer(SECRET, target, body if signed_body is None else signed_body, request.headers["x-failover-signature"])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/inbound/failover/footage":
            body = json.dumps({"segments": self.listing}).encode()
            if "list" in self.forge:  # A list with a segment of the attacker's added, and no valid signature
                planted = [*self.listing, {**self.listing[0], "id": 99}]
                return httpx.Response(200, content=json.dumps({"segments": planted}).encode(), headers={"content-type": "application/json"})
            return httpx.Response(200, content=body, headers={"content-type": "application/json", **self.signed(request, body)})
        if path.startswith("/api/inbound/failover/footage/"):
            segment = int(path.rsplit("/", 1)[1])
            if segment in self.missing:
                body = json.dumps({"detail": "The footage is not available"}).encode()
                headers = {} if "404" in self.forge else self.signed(request, body)
                return httpx.Response(404, content=body, headers=headers)
            content = PLANTED if "file" in self.forge else FOOTAGE
            digest = hashlib.sha256(content).hexdigest()
            summary = failover.footage_summary(hashlib.sha256(FOOTAGE).hexdigest(), len(FOOTAGE))  # What the standby really signed
            headers = {"X-Footage-Sha256": digest, "X-Footage-Size": str(len(content)), **self.signed(request, b"", summary)}
            if "longer" in self.forge:
                content = FOOTAGE + b"and more"
            return httpx.Response(200, content=content, headers=headers)
        if path == "/api/inbound/failover/handback-complete":
            self.completed.append(json.loads(request.content))
            body = b'{"done":true}'
            return httpx.Response(200, content=body, headers={} if "done" in self.forge else self.signed(request, body))
        return httpx.Response(404)


class HandbackCase(FailoverCase):
    def setUp(self):
        super().setUp()
        self.configure("primary")
        failover.learn_of_takeover({"epoch": 1, "takeover_started_at": datetime.now(timezone.utc).isoformat(), "silent_since": None, "segments": 2})
        start = datetime.now(timezone.utc)
        self.listing = [{"id": 11 + number, "camera_id": self.d1, "starts_at": (start + timedelta(seconds=2 * number)).isoformat(),
                         "ends_at": (start + timedelta(seconds=2 * number + 2)).isoformat(), "size_bytes": len(FOOTAGE)} for number in range(2)]

    def handback(self, standby: Standby) -> None:
        real = httpx.AsyncClient

        def client(**options):
            return real(transport=httpx.MockTransport(standby), **options)

        with connect() as db:
            settings = failover.read_settings(db) | {"peer_url": "https://peer.example"}
        with mock.patch.object(failover.httpx, "AsyncClient", client):
            asyncio.run(failover.FailoverManager()._import(settings, SECRET, 1))

    def imported(self) -> list[bytes]:
        from app.recording import recordings_path

        with connect() as db:
            rows = db.execute("SELECT relative_path FROM recording_segments WHERE source='failover' ORDER BY id").fetchall()
        return [(recordings_path() / row[0]).read_bytes() for row in rows]

    def skipped(self) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM failover_imports WHERE segment_id IS NULL").fetchone()[0]

    def episode_state(self) -> str:
        with connect() as db:
            return db.execute("SELECT state FROM failover_episodes WHERE role='primary' AND epoch=1").fetchone()[0]


class GenuineTests(HandbackCase):
    def test_a_standby_that_signs_its_answers_hands_back(self):
        self.handback(Standby(self.listing))
        self.assertEqual(self.imported(), [FOOTAGE, FOOTAGE])
        self.assertEqual(self.episode_state(), "done")

    def test_a_signed_no_such_footage_is_skipped_as_before(self):
        self.handback(Standby(self.listing, missing={11}))
        self.assertEqual((len(self.imported()), self.skipped(), self.episode_state()), (1, 1, "done"))


class ForgedTests(HandbackCase):
    def test_a_forged_list_is_ignored_and_nothing_is_indexed(self):
        self.handback(Standby(self.listing, forge={"list"}))
        self.assertEqual(self.imported(), [])
        self.assertEqual((self.skipped(), self.episode_state()), (0, "handback"))  # Asked for again at the next pass
        self.assertEqual(self.state()["peer_state"], "unverified")

    def test_a_replaced_file_is_refused_never_indexed_or_skipped(self):
        self.handback(Standby(self.listing, forge={"file"}))
        self.assertEqual((self.imported(), self.skipped(), self.episode_state()), ([], 0, "handback"))

    def test_a_file_longer_than_its_signed_size_is_refused(self):
        self.handback(Standby(self.listing, forge={"longer"}))
        self.assertEqual((self.imported(), self.skipped()), ([], 0))

    def test_a_file_whose_signed_size_is_not_the_listed_size_is_refused(self):
        self.listing[0]["size_bytes"] = len(FOOTAGE) - 1
        self.handback(Standby(self.listing))
        self.assertEqual(self.imported(), [])  # The first is refused, and the pass stops to try again

    def test_an_unsigned_no_such_footage_does_not_make_the_primary_drop_it(self):
        self.handback(Standby(self.listing, missing={11}, forge={"404"}))
        self.assertEqual((self.skipped(), self.episode_state()), (0, "handback"))

    def test_an_unsigned_done_does_not_mark_the_handback_done(self):
        standby = Standby(self.listing, forge={"done"})
        self.handback(standby)
        self.assertEqual(len(self.imported()), 2)
        self.assertEqual(len(standby.completed), 1)
        self.assertEqual(self.episode_state(), "handback")
        self.handback(Standby(self.listing))  # The next pass, answered properly
        self.assertEqual(self.episode_state(), "done")

    def test_footage_that_would_take_the_volume_below_its_headroom_is_not_brought_back(self):
        Usage = namedtuple("Usage", "total used free")
        with mock.patch.object(failover.shutil, "disk_usage", return_value=Usage(10**9, 0, 1000)), mock.patch("app.exports.free_floor", return_value=500):
            self.handback(Standby(self.listing))
        self.assertEqual((self.imported(), self.skipped(), self.episode_state()), ([], 0, "handback"))
        self.assertEqual(self.state()["peer_state"], "no_room")
        self.assertIn("not enough free space", self.state()["last_error"])


class StandbySignsTests(FailoverCase):
    def request(self, client, method: str, path: str, body: bytes = b"", **sign_options):
        headers = failover.sign(SECRET, method, path, body, **sign_options)
        return client.request(method, path, content=body, headers=headers), headers

    def test_the_standby_signs_its_list_its_files_its_refusals_and_its_done(self):
        self.configure("standby")
        failover.standby_lease({})
        failover.check_takeover(datetime.now(timezone.utc) + timedelta(seconds=60))
        started = datetime.fromisoformat(self.state()["takeover_started_at"])
        segment = self.add_live_segment(self.d1, started + timedelta(seconds=1))
        with connect() as db:
            db.execute("UPDATE failover_state SET takeover_active=0")
            db.execute("UPDATE failover_episodes SET state='handback'")
        client = TestClient(self.app, base_url=LINK)
        for path in ("/api/inbound/failover/footage?epoch=1", "/api/inbound/failover/footage/999999?epoch=1"):
            answer, headers = self.request(client, "GET", path)
            self.assertTrue(failover.verify_answer(SECRET, path, answer.content, headers["X-Failover-Signature"], answer.headers["x-failover-timestamp"], answer.headers["x-failover-signature"]), path)
        path = f"/api/inbound/failover/footage/{segment}?epoch=1"
        answer, headers = self.request(client, "GET", path)
        summary = failover.footage_summary(answer.headers["x-footage-sha256"], int(answer.headers["x-footage-size"]))
        self.assertTrue(failover.verify_answer(SECRET, path, summary, headers["X-Failover-Signature"], answer.headers["x-failover-timestamp"], answer.headers["x-failover-signature"]))
        self.assertEqual(int(answer.headers["x-footage-size"]), len(answer.content))
        body = json.dumps({"epoch": 1, "imported": 1, "bytes": 1}).encode()
        answer, headers = self.request(client, "POST", "/api/inbound/failover/handback-complete", body)
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertTrue(failover.verify_answer(SECRET, "/api/inbound/failover/handback-complete", answer.content, headers["X-Failover-Signature"],
                                               answer.headers["x-failover-timestamp"], answer.headers["x-failover-signature"]))

    def test_a_refusal_of_a_request_that_was_not_signed_is_not_signed(self):
        self.configure("standby")
        answer = TestClient(self.app, base_url=LINK).get("/api/inbound/failover/footage/1?epoch=1")
        self.assertEqual(answer.status_code, 401)
        self.assertNotIn("x-failover-signature", answer.headers)


class ReplayAndClockTests(FailoverCase):
    def setUp(self):
        super().setUp()
        failover._seen_nonces.clear()

    def test_a_request_sent_twice_is_refused_the_second_time(self):
        self.configure("standby")
        client = TestClient(self.app, base_url=LINK)
        headers = failover.sign(SECRET, "POST", "/api/inbound/failover/lease", b"{}")
        self.assertEqual(client.post("/api/inbound/failover/lease", content=b"{}", headers=headers).status_code, 200)
        replayed = client.post("/api/inbound/failover/lease", content=b"{}", headers=headers)
        self.assertEqual((replayed.status_code, replayed.json()["detail"]), (401, "The request was sent before"))
        self.assertEqual(client.post("/api/inbound/failover/lease", content=b"{}", headers=failover.sign(SECRET, "POST", "/api/inbound/failover/lease", b"{}")).status_code, 200)

    def test_the_one_time_value_cannot_be_changed_without_the_secret(self):
        self.configure("standby")
        headers = failover.sign(SECRET, "POST", "/api/inbound/failover/lease", b"{}")
        headers[failover.NONCE_HEADER] = "0" * 32  # A fresh value, the signature left as it was
        self.assertEqual(TestClient(self.app, base_url=LINK).post("/api/inbound/failover/lease", content=b"{}", headers=headers).status_code, 401)

    def test_an_nvr_not_yet_upgraded_is_refused_but_heard_so_the_standby_does_not_take_over_while_it_records(self):
        self.configure("standby")
        with connect() as db:
            failover._set_state(db, last_renewed_at=None)
        old = failover.sign(SECRET, "POST", "/api/inbound/failover/lease", b"{}")
        old = {key: value for key, value in old.items() if key in ("X-Failover-Timestamp", "X-Failover-Signature")}  # What an older version sends
        answer = TestClient(self.app, base_url=LINK).post("/api/inbound/failover/lease", content=b"{}", headers=old)
        self.assertEqual(answer.status_code, 426)
        self.assertIn("upgrade both NVRs", answer.json()["detail"])
        self.assertIsNotNone(self.state()["last_renewed_at"])
        self.assertIn("not been upgraded", self.state()["last_error"])
        self.assertIsNone(failover.check_takeover(datetime.now(timezone.utc) + timedelta(seconds=5)))

    def test_a_request_signed_right_but_out_of_time_is_reported_as_the_clocks(self):
        self.configure("standby")
        headers = failover.sign(SECRET, "POST", "/api/inbound/failover/lease", b"{}", timestamp=int(time.time()) - 125)
        answer = TestClient(self.app, base_url=LINK).post("/api/inbound/failover/lease", content=b"{}", headers=headers)
        self.assertEqual(answer.status_code, 401)
        self.assertRegex(answer.json()["detail"], r"clocks differ by about 12[56] seconds")  # Signed 125 s ago, read a moment later
        self.assertIn("clocks differ", self.state()["last_error"])
        wrong = failover.sign("not-the-secret-0123456789", "POST", "/api/inbound/failover/lease", b"{}", timestamp=int(time.time()) - 125)
        self.assertEqual(TestClient(self.app, base_url=LINK).post("/api/inbound/failover/lease", content=b"{}", headers=wrong).json()["detail"],
                         "The request is not signed with this NVR's failover secret")  # Without the secret, nothing is said about the clocks

    def test_the_primary_names_the_clock_for_an_answer_signed_right_but_out_of_time(self):
        self.configure("primary")
        path, request = "/api/inbound/failover/lease", "sha256=" + "e" * 64
        body = json.dumps({"granted": True, "state": "watching", "epoch": 0, "handback": None}).encode()

        async def call(*_args, **_kwargs):
            response = httpx.Response(200, content=body, headers={"content-type": "application/json", **failover.sign_answer(SECRET, path, body, request, timestamp=int(time.time()) + 300)})
            response.extensions["failover_request_signature"] = request
            return response

        manager = failover.FailoverManager()
        with connect() as db:
            settings = failover.read_settings(db)
        with mock.patch.object(manager, "_call", call):
            asyncio.run(manager._renew(settings, SECRET))
        self.assertEqual(self.state()["peer_state"], "unverified")
        self.assertRegex(self.state()["last_error"], r"clocks differ by about (299|300) seconds")
