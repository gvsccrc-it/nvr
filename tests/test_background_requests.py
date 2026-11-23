"""What the interface's background polls ask the server for, answered in fewer requests (#536).

The status chip downloaded up to 100 open alarms every 5 seconds to show three numbers; it now asks for the counts alone (``limit=0``).
The status area asked for active and acknowledged incidents in two requests every 15 seconds; ``state=open`` is both in one."""
from __future__ import annotations

import asyncio
from unittest import mock

from app import console
from app.access import Access
from app.incidents import sync_incident
from app.store import connect
from tests.test_alarm_inbox_cost import MINE, Case


class CountsOnlyTests(Case):
    def test_a_counts_only_request_gives_the_counts_and_no_alarms(self):
        self.alarms(400, open_every=3)
        client = self.client()
        full = client.get("/api/alarms?state=open").json()
        counted = client.get("/api/alarms?state=open&limit=0")
        self.assertEqual(counted.status_code, 200, counted.text)
        self.assertEqual(counted.json()["alarms"], [])
        self.assertEqual(counted.json()["counts"], full["counts"])
        self.assertGreater(full["counts"]["new"], 0)
        self.assertLess(len(counted.content), len(client.get("/api/alarms?state=open").content) / 10)
        # For an operator limited to some cameras, the counts are of those cameras' alarms, as in the list
        mine = self.client(Access(cameras=MINE))
        self.assertEqual(mine.get("/api/alarms?state=open&limit=0").json()["counts"], mine.get("/api/alarms?state=open").json()["counts"])

    def test_a_console_asks_each_site_for_one_alarm_which_any_version_accepts_and_leaves_it_out(self):
        asked = []

        async def call(link, method, path, params=None, **kwargs):
            asked.append(params)
            return mock.Mock(status_code=200, json=lambda: {"alarms": [{"id": "x", "track_id": None, "rule_id": None, "occurred_at": "2026-11-23T00:00:00Z",
                                                                        "camera_id": "c1"}], "counts": {"new": 3, "acknowledged": 1, "assigned": 0, "resolved": 9}, "rules": []})

        link = mock.Mock(id="site-1", last_error=None)
        link.name = "North"
        context = console.Context({"state": "open", "limit": 0, "user": None}, None, links=[link])
        context.extra = {"camera_id": None, "rule_id": None}
        local = {"alarms": [], "counts": {"new": 1, "acknowledged": 0, "assigned": 0, "resolved": 2}, "rules": []}
        with mock.patch.object(console, "call", call), mock.patch.object(console, "rewrite", lambda site, alarm: dict(alarm)), \
                mock.patch.object(console, "site_status", lambda link: {"id": link.id}):
            combined = asyncio.run(console.combine_alarms(local, context))
        self.assertEqual(asked, [{"state": "open", "limit": 1}])
        self.assertEqual(combined["alarms"], [])
        self.assertEqual(combined["counts"], {"new": 4, "acknowledged": 1, "assigned": 0, "resolved": 11})


class OpenIncidentsTests(Case):
    def incidents(self) -> dict[str, str]:
        """Four incidents, made after the application has started (its start-up storage check recovers storage incidents it does not see)."""
        made = {}
        with connect() as db:
            for name, camera in (("active", "c01"), ("acknowledged", "c02"), ("recovered", "c03"), ("other camera", "c40")):
                key = f"test:{name}"
                sync_incident(db, incident_key=key, camera_id=camera, fault_type="tiering", summary=f"Incident {name}", active=True)
                made[name] = db.execute("SELECT id FROM incidents WHERE incident_key=?", (key,)).fetchone()[0]
            db.execute("UPDATE incidents SET state='acknowledged' WHERE id=?", (made["acknowledged"],))
            sync_incident(db, incident_key="test:recovered", camera_id="c03", fault_type="tiering", summary="", active=False)
        return made

    def test_open_is_active_and_acknowledged_together_newest_first(self):
        client = self.client()
        made = self.incidents()
        one = client.get("/api/incidents?state=open").json()["incidents"]
        two = client.get("/api/incidents?state=active").json()["incidents"] + client.get("/api/incidents?state=acknowledged").json()["incidents"]
        self.assertEqual({item["id"] for item in one}, {made["active"], made["acknowledged"], made["other camera"]})
        self.assertEqual({item["id"] for item in one}, {item["id"] for item in two})
        self.assertNotIn(made["recovered"], {item["id"] for item in one})
        self.assertEqual([item["last_seen_at"] for item in one], sorted((item["last_seen_at"] for item in one), reverse=True))

    def test_open_follows_the_cameras_a_person_may_see(self):
        mine_client = self.client(Access(cameras=MINE))
        made = self.incidents()
        mine = mine_client.get("/api/incidents?state=open").json()["incidents"]
        self.assertEqual({item["id"] for item in mine}, {made["active"], made["acknowledged"]})

    def test_an_unknown_state_is_still_refused(self):
        self.assertEqual(self.client().get("/api/incidents?state=everything").status_code, 422)


if __name__ == "__main__":
    import unittest

    unittest.main()
