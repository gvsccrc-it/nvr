"""Insights, the heatmap, searches and a rule's test are limited in number and in time, and read only the tracks that can reach their range (#515)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import heavy, plates, rules, search
from app.main import app
from app.principal import Principal, current_user
from app.store import connect, initialize

NOW = datetime.now(timezone.utc).replace(microsecond=0)
REPORT = {"start": (NOW - timedelta(days=1)).isoformat(), "end": NOW.isoformat()}


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = mock.patch.dict(os.environ, {"NVR_DB_PATH": str(Path(self.temporary.name) / "state.sqlite3")})
        environment.start()
        self.addCleanup(environment.stop)
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'keeper','x','administrator')")
            for camera in ("c1", "c2"):
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')", (camera, camera.upper()))

    def client(self):
        app.dependency_overrides[current_user] = lambda: Principal(id=1, username="keeper", role="administrator")
        self.addCleanup(app.dependency_overrides.pop, current_user, None)
        context = TestClient(app)
        self.addCleanup(context.__exit__, None, None, None)
        return context.__enter__()

    def track(self, track_id: str, camera: str, first: datetime, last: datetime, label: str = "person") -> None:
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'m','1','CPU')",
                       (track_id, camera, label, first.isoformat(), last.isoformat()))


class LimitTests(Case):
    def test_more_than_the_limit_at_once_are_told_to_try_again(self):
        client = self.client()
        with mock.patch.object(heavy, "_slots", threading.BoundedSemaphore(1)):
            heavy._slots.acquire()  # One already running
            try:
                for path, extra in (("/api/insights", {}), ("/api/insights/heatmap", {"camera_id": "c1"}), ("/api/ai/search", {}), ("/api/ai/tracks/t/similar", {})):
                    answer = client.get(path, params={**REPORT, **extra})
                    self.assertEqual(answer.status_code, 503, path)
                    self.assertEqual(answer.headers["retry-after"], "5", path)
                    self.assertIn("busy", answer.json()["detail"], path)
                answer = client.post("/api/rules/test", json={"name": "Test", "camera_ids": ["c1"], "classes": ["person"],
                                                              "condition": {"type": "zone_entry", "zone": {"x": 0, "y": 0, "width": 1, "height": 1}}})
                self.assertEqual(answer.status_code, 503, answer.text)
            finally:
                heavy._slots.release()
            self.assertEqual(client.get("/api/insights", params=REPORT).status_code, 200)  # Once it is done, the next is answered
        self.assertEqual(client.get("/api/insights", params=REPORT).status_code, 200)

    def test_a_request_past_its_time_is_stopped_and_answered_503(self):
        with self.assertRaises(HTTPException) as caught, heavy.limited("report", seconds=0.05), connect() as db:
            db.execute("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n) SELECT COUNT(*) FROM (SELECT i FROM n LIMIT 1000000000)").fetchone()
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("took longer than 0.05 seconds", caught.exception.detail)
        with mock.patch.object(heavy, "BUDGET_SECONDS", 0.0):
            answer = self.client().get("/api/insights", params=REPORT)
        self.assertEqual(answer.status_code, 503)
        self.assertIn("choose fewer cameras or a shorter time", answer.json()["detail"])

    def test_a_slot_is_given_back_however_the_request_ends(self):
        with mock.patch.object(heavy, "_slots", threading.BoundedSemaphore(1)):
            for raised in (HTTPException(status_code=404), ValueError("broken"), sqlite3.OperationalError("database is locked")):
                with self.assertRaises(type(raised)), heavy.limited("search"):
                    raise raised
            with heavy.limited("search"):
                pass  # The one slot is free again

    def test_outside_a_limited_request_connections_are_not_stopped(self):
        with connect() as db:
            self.assertEqual(db.execute("WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<200000) SELECT COUNT(*) FROM n").fetchone()[0], 200001)
        self.assertIsNone(heavy.deadline())


class RangeTests(Case):
    def plan(self, statement: str, values: tuple) -> str:
        with connect() as db:
            return " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + statement, values))

    def test_a_search_reads_from_the_longest_track_before_its_start_through_an_index(self):
        self.track("long", "c1", NOW - timedelta(days=3), NOW - timedelta(hours=1))  # Three days long, still going an hour ago
        self.track("short", "c1", NOW - timedelta(hours=2), NOW - timedelta(hours=2) + timedelta(seconds=30))
        statements: list[str] = []
        with connect() as db:
            db.set_trace_callback(statements.append)
            found, _ = search.find_tracks(db, ["c1"], NOW - timedelta(hours=1, minutes=30), NOW, limit=10)
            since = search.began_since(db, ["c1"], NOW - timedelta(hours=1, minutes=30))
        self.assertEqual([item["track_id"] for item in found], ["long"])  # Began three days before the range, still found
        self.assertLess(since, (NOW - timedelta(days=3)).isoformat())
        self.assertGreater(since, (NOW - timedelta(days=3, hours=2)).isoformat())
        query = next(statement for statement in statements if "FROM ai_tracks t" in statement and "first_seen_at>=" in statement)
        plan = self.plan(query, ())
        self.assertIn("ai_tracks_camera_time_idx (camera_id=? AND first_seen_at>? AND first_seen_at<?)", plan)

    def test_a_rule_test_reads_each_camera_through_an_index_range(self):
        statements: list[str] = []
        rule = rules.Rule(id="test", name="Test", enabled=True, camera_ids=frozenset({"c1"}), group_ids=frozenset(), condition_type="zone_entry",
                          condition={"zone": {"x": 0, "y": 0, "width": 1, "height": 1}}, classes=frozenset({"person"}), schedule=None,
                          cooldown=timedelta(0), active_from=NOW - timedelta(days=1))
        real = rules.connect

        def traced():
            db = real()
            db.set_trace_callback(statements.append)
            return db

        with mock.patch.object(rules, "connect", traced):
            rules.replay(rule, NOW - timedelta(days=1), NOW)
        query = next(statement for statement in statements if "CROSS JOIN ai_tracks t" in statement)
        plan = self.plan(query, ())
        self.assertIn("ai_tracks_camera_time_idx (camera_id=? AND first_seen_at>? AND first_seen_at<?)", plan)
        self.assertNotIn("SCAN t ", plan + " ")

    def test_the_longest_track_is_kept_as_tracks_grow(self):
        self.track("a", "c1", NOW - timedelta(minutes=5), NOW - timedelta(minutes=4))
        with connect() as db:
            self.assertAlmostEqual(db.execute("SELECT longest_seconds FROM ai_track_spans WHERE camera_id='c1'").fetchone()[0], 60, places=3)
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                       "VALUES('c1','a',?,'person',0.9,0,0,0.1,0.1,'m','1','CPU')", ((NOW - timedelta(minutes=5)).isoformat(),))
            db.execute("UPDATE ai_tracks SET last_seen_at=? WHERE id='a'", (NOW.isoformat(),))  # Seen again now: ten... five minutes long
            self.assertAlmostEqual(db.execute("SELECT longest_seconds FROM ai_track_spans WHERE camera_id='c1'").fetchone()[0], 300, places=3)
            db.execute("UPDATE ai_tracks SET last_seen_at=? WHERE id='a'", ((NOW - timedelta(minutes=4)).isoformat(),))  # Shorter: the longest stays
            self.assertAlmostEqual(db.execute("SELECT longest_seconds FROM ai_track_spans WHERE camera_id='c1'").fetchone()[0], 300, places=3)
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES('odd','c2','person','yesterday','today','m','1','CPU')")
            self.assertEqual(search.began_since(db, ["c2"], NOW), "")  # A time that cannot be read: everything is read rather than miss it

    def test_the_migration_works_out_the_tracks_already_there(self):
        self.track("a", "c1", NOW - timedelta(hours=2), NOW)
        self.track("b", "c2", NOW - timedelta(minutes=1), NOW)
        with connect() as db:
            db.execute("DELETE FROM ai_track_spans")
            from app.migrations import m0097_track_spans
            m0097_track_spans.upgrade(db)
            spans = dict(db.execute("SELECT camera_id,longest_seconds FROM ai_track_spans").fetchall())
        self.assertAlmostEqual(spans["c1"], 7200, places=3)
        self.assertAlmostEqual(spans["c2"], 60, places=3)


class PlateTests(Case):
    def test_a_plate_rule_test_reads_its_watch_list_once(self):
        with connect() as db:
            db.execute("INSERT INTO plate_watch_lists(id,name,kind) VALUES('w','Barred','deny')")
            db.executemany("INSERT INTO plate_watch_entries(id,list_id,pattern) VALUES(?,'w',?)", [(f"e{number}", f"ZZ{number:04}") for number in range(500)] + [("hit", "AB12CDE")])
            self.track("t", "c1", NOW - timedelta(hours=3), NOW - timedelta(hours=2), label="car")
            db.executemany("INSERT INTO plate_reads(id,camera_id,track_id,plate,folded,confidence,read_at,box_json,reader) VALUES(?,'c1','t',?,?,0.9,?,'{}','r')",
                           [(f"r{number}", plate, plate, (NOW - timedelta(hours=2, seconds=number)).isoformat())
                            for number, plate in enumerate(["AB12CDE"] + [f"XY{number:04}" for number in range(60)])])
        rule = rules.Rule(id="test", name="Test", enabled=True, camera_ids=frozenset({"c1"}), group_ids=frozenset(), condition_type="plate",
                          condition={"watch_list_id": "w"}, classes=frozenset(), schedule=None, cooldown=timedelta(0), active_from=NOW - timedelta(days=1))
        with mock.patch.object(plates, "load_watch_list", wraps=plates.load_watch_list) as loads:
            events = rules.replay(rule, NOW - timedelta(days=1), NOW)
        self.assertEqual(loads.call_count, 1)
        self.assertEqual([event["label"] for event in events], [f"{rules.PLATE_LABEL_PREFIX}AB12CDE"])


if __name__ == "__main__":
    unittest.main()
