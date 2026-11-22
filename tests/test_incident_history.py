"""Incident history is bounded and the incident list is cheap to poll (#516).

Nothing removed an incident or its events, and every open tab asked every 15 seconds for every event of every incident; a person limited
to some cameras had up to 5,000 incidents read, each with all its events, and filtered afterwards.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import incidents
from app.access import Access
from app.main import app
from app.principal import Principal, current_user
from app.store import connect, initialize

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = mock.patch.dict(os.environ, {"NVR_DB_PATH": str(Path(self.temporary.name) / "state.sqlite3")})
        environment.start()
        self.addCleanup(environment.stop)
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'keeper','x','administrator'),(2,'guard','x','operator')")
            for camera in ("c1", "c2", "c3"):
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (camera, camera.upper()))

    def incident(self, key: str, camera: str | None, *, state: str = "active", seen: datetime = NOW, events: int = 1, events_at: datetime | None = None,
                 recovered: datetime | None = None) -> str:
        """An incident with ``events`` events, one a second up to ``events_at`` (its last-seen time by default)."""
        incident_id = f"i-{key}"
        last = events_at or seen
        with connect() as db:
            db.execute("INSERT INTO incidents(id,incident_key,camera_id,fault_type,summary,state,fault_active,first_seen_at,last_seen_at,recovered_at,event_count) "
                       "VALUES(?,?,?,'recording_failure',?,?,?,?,?,?,?)",
                       (incident_id, key, camera, f"fault {key}", state, int(state != "recovered"), (last - timedelta(seconds=events)).isoformat(), seen.isoformat(),
                        recovered.isoformat() if recovered else None, events))
            db.execute(f"""
                WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i < {events - 1})
                INSERT INTO incident_events(event_id,incident_id,event_type,occurred_at,details_json)
                SELECT ?||'-'||i,?,CASE i WHEN 0 THEN 'raised' WHEN {events - 1} THEN ? ELSE CASE i % 2 WHEN 1 THEN 'recovered' ELSE 'recurred' END END,
                       strftime('%Y-%m-%dT%H:%M:%S+00:00',?,'-'||({events - 1}-i)||' seconds'),'{{"n":'||i||'}}' FROM n""",
                       (incident_id, incident_id, "recovered" if state == "recovered" else "recurred", last.strftime("%Y-%m-%d %H:%M:%S")))
        return incident_id

    def client(self, access: Access | None = None):
        person = Principal(id=1, username="keeper", role="administrator") if access is None else Principal(id=2, username="guard", role="operator", access=access)
        app.dependency_overrides[current_user] = lambda: person
        self.addCleanup(app.dependency_overrides.pop, current_user, None)
        context = TestClient(app)
        self.addCleanup(context.__exit__, None, None, None)
        return context.__enter__()


class ListTests(Case):
    def test_an_incident_with_ten_thousand_events_is_listed_with_its_newest_few(self):
        flapping = self.incident("flapping", "c1", events=10_000)
        response = self.client().get("/api/incidents?state=active")
        self.assertLess(len(response.content), 5_000)  # Was about 1 MB
        # By its id: the app the test starts can raise incidents of its own (in CI, one with a single event came first)
        incident = next(item for item in response.json()["incidents"] if item["id"] == flapping)
        self.assertEqual(incident["events_total"], 10_000)
        self.assertEqual([event["details"]["n"] for event in incident["events"]], [9995, 9996, 9997, 9998, 9999])  # Newest, oldest first

    def test_the_list_runs_the_same_two_statements_however_many_incidents_and_events(self):
        for number in range(50):
            self.incident(f"k{number}", "c1", seen=NOW - timedelta(minutes=number), events=200)
        statements: list[str] = []
        with connect() as db:
            db.set_trace_callback(statements.append)
            listed = incidents.list_incidents(db)
        self.assertEqual(len(listed), 50)
        self.assertTrue(all(len(item["events"]) == incidents.EVENTS_SHOWN for item in listed))
        self.assertEqual(len(statements), 2)

    def test_the_list_and_a_restricted_list_read_through_an_index(self):
        with connect() as db:
            for state, cameras in ((None, None), ("active", None), (None, frozenset({"c1"})), ("active", frozenset({"c1", "c2"}))):
                with self.subTest(state=state, cameras=cameras):
                    statements: list[str] = []
                    db.set_trace_callback(statements.append)
                    incidents.list_incidents(db, state=state, cameras=cameras)
                    db.set_trace_callback(None)
                    plan = " ".join(row[3] for row in db.execute(f"EXPLAIN QUERY PLAN {statements[0]}"))
                    self.assertNotIn("SCAN incidents", plan.replace("SCAN incidents USING INDEX", "index"))
                    if cameras is None:
                        self.assertNotIn("TEMP B-TREE FOR ORDER BY", plan)  # Read newest first, and stopped at the page
                    else:
                        self.assertIn("USING INDEX incidents_camera_seen_idx (camera_id=?)", plan)  # Only their cameras' incidents are read

    def test_a_restricted_operator_sees_their_incidents_however_many_newer_ones_others_have(self):
        mine = self.incident("mine", "c1", seen=NOW - timedelta(days=1))
        for number in range(6_000):  # Newer than theirs; the old route read at most 5,000 and filtered afterwards
            with connect() as db:
                db.execute("INSERT INTO incidents(id,incident_key,camera_id,fault_type,summary,state,fault_active,first_seen_at,last_seen_at) "
                           "VALUES(?,?,?,'recording_failure','other','active',1,?,?)", (f"o{number}", f"o{number}", "c2" if number % 2 else None, NOW.isoformat(), NOW.isoformat()))
        listed = self.client(Access(cameras=frozenset({"c1"}))).get("/api/incidents").json()["incidents"]
        self.assertEqual([incident["id"] for incident in listed], [mine])
        self.assertEqual(self.client(Access(cameras=frozenset())).get("/api/incidents").json()["incidents"], [])


class HistoryTests(Case):
    def test_the_whole_history_comes_in_pages_each_event_once_and_in_order(self):
        incident_id = self.incident("flapping", "c1", events=10_000)
        client, seen, before, pages = self.client(), [], None, 0
        while True:
            page = client.get(f"/api/incidents/{incident_id}", params={"before": before} if before else {}).json()
            pages += 1
            self.assertLessEqual(len(page["events"]), incidents.HISTORY_PAGE)
            seen = [event["details"]["n"] for event in page["events"]] + seen
            before = page["next"]
            if before is None:
                break
        self.assertEqual((pages, seen), (20, list(range(10_000))))

    def test_an_unknown_incident_is_not_found(self):
        self.assertEqual(self.client().get("/api/incidents/nothing").status_code, 404)


class PruneTests(Case):
    def setUp(self):
        super().setUp()
        with connect() as db:
            self.days = incidents_history_days(db)

    def test_recovered_incidents_past_the_period_go_with_their_events_and_the_rest_stay(self):
        old = NOW - timedelta(days=self.days + 1)
        gone = self.incident("gone", "c1", state="recovered", seen=old, events=1_200, recovered=old)  # More events than one batch
        recent = self.incident("recent", "c1", state="recovered", seen=NOW - timedelta(days=1), recovered=NOW - timedelta(days=1))
        open_old = self.incident("open", "c2", seen=old, events=3, events_at=old)  # Active: never removed, however old
        acknowledged = self.incident("seen", "c2", state="acknowledged", seen=old, events_at=old)
        self.assertEqual(incidents.prune_history(NOW), {"removed_incidents": 1, "removed_events": 4})
        with connect() as db:
            self.assertEqual({row[0] for row in db.execute("SELECT id FROM incidents")}, {recent, open_old, acknowledged})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM incident_events WHERE incident_id=?", (gone,)).fetchone()[0], 0)

    def test_old_events_of_an_incident_that_stays_are_removed_and_counted(self):
        flapping = self.incident("flapping", "c1", events=5_000, events_at=NOW - timedelta(days=self.days) + timedelta(seconds=999))  # 4,000 before the cut-off
        done = incidents.prune_history(NOW)
        self.assertEqual(done, {"removed_incidents": 0, "removed_events": 4_000})
        view = self.client().get(f"/api/incidents/{flapping}").json()
        self.assertEqual((view["events_total"], view["events_removed"]), (5_000, 4_000))
        self.assertIsNotNone(view["events_removed_before"])
        self.assertEqual(len(view["events"]), 500)
        listed = next(item for item in self.client().get("/api/incidents").json()["incidents"] if item["id"] == flapping)
        self.assertEqual(listed["events_total"], 5_000)

    def test_a_pass_with_nothing_due_takes_no_write_lock(self):
        self.incident("recent", "c1", state="recovered", seen=NOW - timedelta(days=1), recovered=NOW - timedelta(days=1))
        statements: list[str] = []
        real = connect

        def traced():
            db = real()
            db.set_trace_callback(statements.append)
            return db

        with mock.patch("app.store.connect", traced):
            self.assertEqual(incidents.prune_history(NOW), {"removed_incidents": 0, "removed_events": 0})
        self.assertFalse([statement for statement in statements if "BEGIN" in statement or "DELETE" in statement or "UPDATE" in statement])

    def test_the_period_is_the_alarm_history_setting(self):
        with connect() as db:
            db.execute("UPDATE alarm_settings SET history_days=10 WHERE singleton_id=1")
            if not db.total_changes:
                db.execute("INSERT INTO alarm_settings(singleton_id,history_days) VALUES(1,10)")
        self.incident("eleven", "c1", state="recovered", seen=NOW - timedelta(days=11), recovered=NOW - timedelta(days=11))
        self.incident("nine", "c1", state="recovered", seen=NOW - timedelta(days=9), recovered=NOW - timedelta(days=9))
        self.assertEqual(incidents.prune_history(NOW)["removed_incidents"], 1)


class CountTests(Case):
    def test_each_event_is_counted_on_its_incident(self):
        with connect() as db:
            for active in (True, False, True, False, True):
                incidents.sync_incident(db, incident_key="k", camera_id="c1", fault_type="recording_failure", summary="s", active=active)
            row = db.execute("SELECT id,event_count FROM incidents").fetchone()
            self.assertEqual(row["event_count"], db.execute("SELECT COUNT(*) FROM incident_events WHERE incident_id=?", (row["id"],)).fetchone()[0])
            self.assertEqual(row["event_count"], 5)


def incidents_history_days(db) -> int:
    from app.alarms import settings
    return settings(db)["history_days"]


if __name__ == "__main__":
    unittest.main()
