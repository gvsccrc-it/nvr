"""The alarm inbox poll is as cheap for an operator limited to some cameras as for an administrator (#517).

Every open tab polls the inbox every five seconds. For a restricted operator the list read every alarm of their cameras and sorted them,
and for everyone the counts and the rule filter read every alarm, resolved history included: at 200,000 alarms an administrator's poll took
15 ms and an operator's limited to 25 of 50 cameras 294 ms.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.access import Access
from app.main import app
from app.principal import Principal, current_user
from app.store import connect, initialize

CAMERAS = [f"c{number:02}" for number in range(50)]
MINE = frozenset(CAMERAS[:25])
COUNTS = "SELECT camera_id,rule_key,rule_name,state,count FROM alarm_counts ORDER BY 1,2,3,4"
WORKED_OUT = ("SELECT camera_id,COALESCE(rule_id,''),rule_name,state,COUNT(*) FROM alarms GROUP BY camera_id,COALESCE(rule_id,''),rule_name,state "
              "ORDER BY 1,2,3,4")


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
            for camera in CAMERAS:
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (camera, camera.upper()))
            for rule in ("gate", "dock"):
                db.execute("INSERT INTO rules(id,name,condition_type,condition_json,classes_json,active_from) VALUES(?,?,'zone_entry','{}','[]','2026-01-01T00:00:00Z')", (rule, rule.title()))

    def alarms(self, count: int, open_every: int = 0) -> None:
        """``count`` alarms over the fifty cameras and ninety days, newest last; every ``open_every``th still open, the rest resolved."""
        with connect() as db:
            db.execute(f"""
                WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i < {count - 1})
                INSERT INTO alarms(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,state,created_at,updated_at)
                SELECT 'a'||i, CASE i % 3 WHEN 0 THEN 'gate' WHEN 1 THEN 'dock' ELSE NULL END, CASE i % 3 WHEN 0 THEN 'Gate' WHEN 1 THEN 'Dock' ELSE 'Old rule' END,
                       printf('c%02d', i % 50), 't', 'person', 'zone_entry', t, '{{}}', t, t,
                       CASE WHEN {open_every} > 0 AND i % {open_every} = 0 THEN CASE i % 7 WHEN 0 THEN 'acknowledged' ELSE 'new' END ELSE 'resolved' END, t, t
                FROM (SELECT i, strftime('%Y-%m-%dT%H:%M:%f+00:00','2026-11-22 12:00:00','-' || (({count} - i) * 7776000 / {count}) || ' seconds') AS t FROM n)""")

    def client(self, access: Access | None = None):
        person = Principal(id=1, username="keeper", role="administrator") if access is None else Principal(id=2, username="guard", role="operator", access=access)
        app.dependency_overrides[current_user] = lambda: person
        self.addCleanup(app.dependency_overrides.pop, current_user, None)
        context = TestClient(app)
        self.addCleanup(context.__exit__, None, None, None)
        return context.__enter__()


class CostTests(Case):
    def test_a_restricted_poll_costs_within_a_small_factor_of_an_administrators(self):
        self.alarms(200_000, open_every=667)  # 300 open
        timings = {}
        for name, access in (("administrator", None), ("restricted", Access(cameras=MINE))):
            client = self.client(access)
            client.get("/api/alarms")  # Warm
            runs = []
            for _ in range(5):
                started = time.perf_counter()
                answer = client.get("/api/alarms").json()
                runs.append(time.perf_counter() - started)
            timings[name] = min(runs)
            self.assertEqual(sum(answer["counts"].values()), 200_000 if access is None else 100_000)
        # Measured here, the whole request: 33 ms and 302 ms before, about 20 ms each now
        self.assertLess(timings["restricted"], 3 * timings["administrator"] + 0.02, timings)

    def test_the_default_view_reads_the_open_alarms_index_and_the_counts_read_no_alarm(self):
        self.alarms(2_000, open_every=10)
        statements: list[str] = []
        real = connect

        def traced():
            db = real()
            db.set_trace_callback(statements.append)
            return db

        with mock.patch("app.api.alarms.connect", traced):
            for access in (None, Access(cameras=MINE)):
                self.client(access).get("/api/alarms")
        inbox = [statement for statement in statements if statement.lstrip().startswith("SELECT") and ("FROM alarms" in statement or "alarm_counts" in statement)]
        self.assertEqual(len(inbox), 6, inbox)  # List, counts, rules, for each person
        with connect() as db:
            for statement in inbox:
                plan = " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + statement))
                if "FROM alarms" in statement:
                    self.assertIn("alarms_open_order_idx", plan, statement)
                    self.assertNotIn("TEMP B-TREE", plan, statement)  # Read in order from the index, not sorted
                else:  # A few rows a camera; the rule filter sorts its handful of rules by name
                    self.assertNotIn(" alarms ", f" {plan} ", statement)  # The counts and the rule filter read no alarm


class ListTests(Case):
    def test_a_restricted_operator_sees_and_counts_only_their_cameras_alarms(self):
        self.alarms(1_000, open_every=4)
        everything = self.client().get("/api/alarms?limit=500").json()
        mine = self.client(Access(cameras=MINE)).get("/api/alarms?limit=500").json()
        self.assertTrue(mine["alarms"])
        self.assertTrue(all(alarm["camera_id"] in MINE for alarm in mine["alarms"]))
        self.assertEqual(len(mine["alarms"]), len([alarm for alarm in everything["alarms"] if alarm["camera_id"] in MINE]))
        with connect() as db:
            for state in ("new", "acknowledged", "resolved"):
                self.assertEqual(mine["counts"][state], db.execute(
                    f"SELECT COUNT(*) FROM alarms WHERE state=? AND camera_id IN ({','.join('?' * len(MINE))})", (state, *sorted(MINE))).fetchone()[0])
        self.assertEqual(self.client(Access(cameras=frozenset())).get("/api/alarms").json()["counts"], {"new": 0, "acknowledged": 0, "assigned": 0, "resolved": 0})

    def test_the_rule_filter_and_the_counts_follow_the_camera_and_rule_filters(self):
        self.alarms(300, open_every=2)
        client = self.client()
        self.assertEqual(client.get("/api/alarms").json()["rules"], [{"id": "dock", "name": "Dock"}, {"id": "gate", "name": "Gate"}])
        with connect() as db:
            for query, where, values in (("camera_id=c03", "camera_id=?", ("c03",)), ("rule_id=gate", "rule_id=?", ("gate",)),
                                         ("camera_id=c03&rule_id=gate", "camera_id=? AND rule_id=?", ("c03", "gate"))):
                counts = client.get(f"/api/alarms?{query}").json()["counts"]
                for state in ("new", "acknowledged", "resolved"):
                    self.assertEqual(counts[state], db.execute(f"SELECT COUNT(*) FROM alarms WHERE state=? AND {where}", (state, *values)).fetchone()[0], query)


class KeptCountTests(Case):
    def assertCounted(self):
        with connect() as db:
            self.assertEqual([tuple(row) for row in db.execute(COUNTS)], [tuple(row) for row in db.execute(WORKED_OUT)])

    def test_the_counts_follow_every_change_to_the_alarms(self):
        self.alarms(500, open_every=3)
        self.assertCounted()
        with connect() as db:
            db.execute("UPDATE alarms SET state='resolved' WHERE id IN ('a0','a3','a6')")
            db.execute("UPDATE alarms SET rule_name='Gate (renamed)' WHERE id='a9'")
            db.execute("UPDATE alarms SET camera_id='c40' WHERE id='a12'")
            db.execute("DELETE FROM alarms WHERE id IN ('a15','a16')")
        self.assertCounted()
        with connect() as db:
            db.execute("DELETE FROM rules WHERE id='gate'")  # Its alarms keep their name; rule_id becomes NULL
            db.execute("DELETE FROM cameras WHERE id='c05'")  # Its alarms go with it
        self.assertCounted()
        with connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM alarm_counts WHERE count<=0").fetchone())  # Emptied rows go
            self.assertFalse(db.execute("SELECT 1 FROM alarm_counts WHERE camera_id='c05'").fetchone())

    def test_the_migration_counts_the_alarms_already_there(self):
        self.alarms(400, open_every=5)
        with connect() as db:
            db.execute("DELETE FROM alarm_counts")
            from app.migrations import m0096_alarm_counts
            m0096_alarm_counts.upgrade(db)
        self.assertCounted()


if __name__ == "__main__":
    unittest.main()
