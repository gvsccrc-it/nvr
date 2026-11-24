"""Bringing a long takeover's footage back costs the same for each file however long the takeover was, and the primary keeps its lease while
it does (#510).

The standby found each file it was asked for by reading the whole list of the takeover's segments, so the work grew with the square of
their number (200 cameras for 8 hours is 96,000 segments, about 9 billion row reads in all), and the primary opened a new connection per
file and sent no renewal until the whole import was done."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

from app import failover
from app.store import connect
from tests.test_failover import SECRET, FailoverCase
from tests.test_failover_handback_signed import FOOTAGE, HandbackCase, Standby


class StandbyLookupTests(FailoverCase):
    def takeover(self, segments: int) -> tuple[int, datetime]:
        """A takeover whose standby recorded this many segments (rows only: the cost measured is the lookup's)."""
        self.configure("standby")
        failover.standby_lease({})
        failover.check_takeover(datetime.now(timezone.utc) + timedelta(seconds=60))
        started = datetime.fromisoformat(self.state()["takeover_started_at"])
        with connect() as db:
            db.executemany("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes,source) VALUES(?,?,?,?,1,'live')",
                           ((self.d1, f"{self.d1}/bulk-{number}.mp4", (started + timedelta(seconds=number)).isoformat(), (started + timedelta(seconds=number + 1)).isoformat())
                            for number in range(segments)))
        return self.add_live_segment(self.d1, started + timedelta(seconds=segments + 5)), started

    def steps(self, segment_id: int) -> int:
        """The database's work to find one file of the takeover: virtual machine steps, not time, so the measure does not depend on the machine."""
        counted = {"steps": 0}
        real = failover.connect

        def counting():
            context = real()
            db = context.__enter__()

            def tick():
                counted["steps"] += 1
                return 0

            db.set_progress_handler(tick, 1)

            class Wrapped:
                def __enter__(self):
                    return db

                def __exit__(self, *exc):
                    return context.__exit__(*exc)

            return Wrapped()

        with mock.patch.object(failover, "connect", counting):
            failover.standby_footage_file(1, segment_id)
        return counted["steps"]

    def test_finding_one_file_does_not_grow_with_the_takeover(self):
        small, _ = self.takeover(10)
        few = self.steps(small)
        with connect() as db:
            db.execute("DELETE FROM recording_segments WHERE relative_path LIKE ?", (f"{self.d1}/bulk-%",))
            db.execute("DELETE FROM failover_episodes")
            failover._set_state(db, takeover_active=0, epoch=0)
        large, _ = self.takeover(20_000)
        many = self.steps(large)
        self.assertLess(many, few * 2, f"{few} steps with 10 segments, {many} with 20,000")

    def test_a_segment_outside_the_takeover_is_still_refused(self):
        inside, started = self.takeover(3)
        before = self.add_live_segment(self.d1, started - timedelta(minutes=10), name="before.mp4")
        imported = self.add_live_segment(self.d1, started + timedelta(seconds=30), name="imported.mp4")
        with connect() as db:
            db.execute("UPDATE recording_segments SET source='failover' WHERE id=?", (imported,))
        self.assertTrue(failover.standby_footage_file(1, inside)[0].name.startswith("segment-"))  # Its own footage is served
        for outside in (before, imported, 999_999):
            with self.assertRaises(failover.PeerError) as refused:
                failover.standby_footage_file(1, outside)
            self.assertEqual(refused.exception.status, 404)
        # A later takeover's footage is that takeover's, not this one's (#498)
        with connect() as db:
            db.execute("UPDATE failover_state SET takeover_active=0")
            db.execute("UPDATE failover_episodes SET state='handback' WHERE epoch=1")
        failover.check_takeover(datetime.now(timezone.utc) + timedelta(seconds=120))
        later = self.add_live_segment(self.d1, datetime.fromisoformat(self.state()["takeover_started_at"]) + timedelta(seconds=1), name="later.mp4")
        with self.assertRaises(failover.PeerError):
            failover.standby_footage_file(1, later)
        self.assertEqual(failover.standby_footage_file(2, later)[0].name, "later.mp4")


class ImportConnectionTests(HandbackCase):
    def test_one_connection_carries_the_import_and_the_lease_is_renewed_between_files(self):
        clients: list[list[str]] = []
        standby = Standby(self.listing)
        real = httpx.AsyncClient

        def client(**options):
            served: list[str] = []
            clients.append(served)

            def handler(request: httpx.Request) -> httpx.Response:
                served.append(request.url.path)
                if request.url.path == "/api/inbound/failover/lease":
                    body = json.dumps({"granted": True, "state": "handback", "lease_seconds": 5, "epoch": 1,
                                       "handback": {"epoch": 1, "takeover_started_at": datetime.now(timezone.utc).isoformat(), "silent_since": None, "segments": 2}}).encode()
                    return httpx.Response(200, content=body, headers=standby.signed(request, body))
                return standby(request)

            options.pop("transport", None)  # The stand-in standby replaces the network, the pinned one included (#502)
            return real(transport=httpx.MockTransport(handler), **options)

        with connect() as db:
            settings = failover.read_settings(db) | {"peer_url": "https://peer.example"}
        manager = failover.FailoverManager()
        with mock.patch.object(failover.httpx, "AsyncClient", client), mock.patch.object(manager, "_renewal_due", return_value=True):
            asyncio.run(manager._import(settings, SECRET, 1))
        handback = [served for served in clients if any(path != "/api/inbound/failover/lease" for path in served)]
        self.assertEqual(len(handback), 1, clients)  # The list, both files and "complete": one client, one connection kept open
        self.assertEqual([path.rsplit("/", 1)[-1] if "/footage/" in path else path for path in handback[0]],
                         ["/api/inbound/failover/footage", "11", "12", "/api/inbound/failover/handback-complete"])
        renewals = [served for served in clients if served == ["/api/inbound/failover/lease"]]
        self.assertEqual(len(renewals), 2)  # Before each file, as the renewal fell due
        self.assertEqual(self.imported(), [FOOTAGE, FOOTAGE])
        self.assertEqual(self.episode_state(), "done")
        self.assertEqual(hashlib.sha256(self.imported()[0]).hexdigest(), hashlib.sha256(FOOTAGE).hexdigest())
