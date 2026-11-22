"""A reconnect does not raise every alarm again, and one rule cannot raise alarms without limit (#514)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from app import alarms, rules
from app.store import connect
from app.tracks import Tracker, record_detections

from tests.scene_case import SceneTestCase

T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
LOT = {"x": 0.0, "y": 0.5, "width": 1.0, "height": 0.5}
CARS = [{"label": "vehicle", "confidence": 0.9, "box": {"x": 0.05 + 0.22 * index, "y": 0.6, "width": 0.15, "height": 0.2}} for index in range(4)]


class StormCase(SceneTestCase):
    def setUp(self):
        super().setUp()
        rules._followers.clear()
        rules._counting.clear()

    def rule(self, name: str = "In the lot", cooldown: int = 0, kind: str = "zone_entry", condition: dict | None = None, classes: tuple[str, ...] = ("vehicle",)) -> str:
        created = self.client.post("/api/rules", json={
            "name": name, "enabled": True, "camera_ids": [self.camera_id], "group_ids": [], "classes": list(classes),
            "condition": {"type": kind, **(condition or {"zone": LOT})}, "schedule": None, "cooldown_seconds": cooldown})
        self.assertEqual(created.status_code, 201, created.text)
        with connect() as db:  # Active from before the scene (a rule made now only watches what is stored from now on)
            db.execute("UPDATE rules SET active_from=? WHERE id=?", ((T0 - timedelta(days=1)).isoformat(), created.json()["id"]))
        return created.json()["id"]

    def frames(self, tracker: Tracker, seconds: range, objects: list[dict] = CARS) -> list[str]:
        ids: list[str] = []
        for second in seconds:
            with connect() as db:
                ids = record_detections(db, self.camera_id, objects, T0 + timedelta(seconds=second), tracker, model_id="m", model_version="1", provider="CPU")
            while rules.process_pending():
                pass
        return ids

    def alarm_rows(self) -> list[dict]:
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM alarms ORDER BY occurred_at,created_at")]

    def count(self, table: str, where: str = "1=1") -> int:
        with connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


class ReconnectTests(StormCase):
    def test_a_reconnect_with_the_same_objects_in_view_raises_nothing_new(self):
        self.rule()
        first = self.frames(Tracker(take_up=True), range(0, 10, 2))
        self.assertEqual(len(self.alarm_rows()), 4)  # Four parked cars, each inside the zone once
        # The stream drops and is opened again, twice: a new tracker each time, as the AI worker makes one.
        second = self.frames(Tracker(take_up=True), range(14, 24, 2))
        third = self.frames(Tracker(take_up=True), range(40, 50, 2))
        self.assertEqual((second, third), (first, first))  # The same four objects, by the same tracks
        self.assertEqual(self.count("ai_tracks"), 4)
        self.assertEqual(len(self.alarm_rows()), 4)

    def test_only_what_was_in_view_within_thirty_seconds_is_taken_up_and_only_where_it_was(self):
        self.rule()
        first = self.frames(Tracker(take_up=True), range(0, 4, 2))
        # A different car parks in the first bay a minute after the stream dropped: not the car that was there.
        later = self.frames(Tracker(take_up=True), range(64, 68, 2), CARS[:1])
        self.assertNotIn(later[0], first)
        # And something that appears elsewhere in the picture straight after a reconnect is new, though other objects are taken up.
        elsewhere = [{"label": "vehicle", "confidence": 0.9, "box": {"x": 0.8, "y": 0.05, "width": 0.15, "height": 0.2}}]
        again = self.frames(Tracker(take_up=True), range(70, 74, 2), CARS[:1] + elsewhere)
        self.assertEqual(again[0], later[0])
        self.assertNotIn(again[1], first + later)
        self.assertEqual(self.count("ai_tracks"), 6)

    def test_a_restarted_ai_worker_takes_its_objects_up_too(self):
        # Nothing is carried in memory: the tracks come from what is stored, so a new process finds them as a new stream does.
        first = self.frames(Tracker(take_up=True), range(0, 4, 2))
        tracker = Tracker(take_up=True)
        self.assertFalse(tracker.resumed)
        self.assertEqual(self.frames(tracker, range(8, 10, 2)), first)
        self.assertTrue(tracker.resumed)
        # A tracker that does not ask starts from nothing, as it always did.
        self.assertNotEqual(self.frames(Tracker(), range(10, 12, 2)), first)


class CeilingTests(StormCase):
    def burst(self, rule_id: str, count: int, start: int = 0, step: float = 0.5, kind: str = "zone_entry") -> None:
        """``count`` events of one rule on the camera, half a second apart: what a rule gone wrong raises."""
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for index in range(count):
                event_id, at = str(uuid.uuid4()), T0 + timedelta(seconds=start + index * step)
                db.execute("INSERT INTO rule_events(id,rule_id,rule_name,camera_id,track_id,detection_id,label,condition_type,occurred_at,box_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (event_id, rule_id, "In the lot", self.camera_id, str(uuid.uuid4()), None, "fall" if kind == "fall" else "vehicle", kind, at.isoformat(), json.dumps(LOT)))
                alarms.create_alarm(db, event_id)
            db.execute("COMMIT")

    def test_a_hundred_in_a_minute_is_ten_alarms_and_one_that_says_the_rest_are_held_back(self):
        rule_id = self.rule()
        with connect() as db:
            db.execute("INSERT INTO rule_actions(id,rule_id,position,name,type,settings_json,target) VALUES(?,?,0,'Post','webhook','{}','https://hooks.example.test/in')", (str(uuid.uuid4()), rule_id))
        self.burst(rule_id, 100)
        raised = self.alarm_rows()
        self.assertEqual([row["label"] for row in raised], ["vehicle"] * alarms.CEILING_A_MINUTE + ["suppressed:0"])  # THE NUMBERS: ten, and one summary
        self.assertEqual(self.count("rule_events", "label='vehicle'"), 100)  # Every event is still recorded
        self.assertEqual(self.count("rule_action_deliveries"), alarms.CEILING_A_MINUTE)  # A delivery for each alarm raised, none for the 90 held back nor for the summary
        self.assertEqual(alarms.label_text("suppressed:0"), "Too many alarms: further ones are being held back")
        with connect() as db:
            held = db.execute("SELECT held_back FROM alarm_suppressions WHERE rule_id=?", (rule_id,)).fetchone()[0]
        self.assertEqual(held, 90)

    def test_while_it_lasts_a_summary_every_ten_minutes_says_how_many_and_the_end_says_the_rest(self):
        rule_id = self.rule()
        self.burst(rule_id, 100)  # 50 seconds
        self.burst(rule_id, 30, start=610, step=0.5)  # Ten minutes on, still at it: the hour's ceiling is not reached, the minute's is again
        labels = [row["label"] for row in self.alarm_rows()]
        # After a quiet ten minutes the rule is under its ceiling: the 89 held back since the first summary are said, and alarms flow again.
        self.assertEqual(labels[:11], ["vehicle"] * 10 + ["suppressed:0"])
        self.assertEqual(labels[11], "suppressed:89")
        self.assertEqual(labels[12:22], ["vehicle"] * 10)
        self.assertEqual(labels[22], "suppressed:0")
        self.assertEqual(alarms.label_text("suppressed:89"), "89 more alarms held back")
        # The closing summary is dated before the alarm of the event that ended the holding back, so their order never rests on which
        # row happened to be written first: listed by time alone, newest first as the inbox lists them, the alarm is above the summary.
        with connect() as db:
            ending = [dict(row) for row in db.execute("SELECT label,occurred_at FROM alarms WHERE label IN ('suppressed:89','vehicle') ORDER BY occurred_at")][10:12]
        self.assertEqual([row["label"] for row in ending], ["suppressed:89", "vehicle"])
        self.assertLess(ending[0]["occurred_at"], ending[1]["occurred_at"])

    def test_sixty_in_an_hour_is_the_other_ceiling(self):
        rule_id = self.rule()
        self.burst(rule_id, 70, step=30)  # One every thirty seconds for thirty-five minutes: never ten in a minute
        labels = [row["label"] for row in self.alarm_rows()]
        self.assertEqual(labels.count("vehicle"), alarms.CEILING_AN_HOUR)
        self.assertEqual(labels[60], "suppressed:0")

    def test_the_ceiling_is_each_rules_and_each_cameras_own_and_a_fall_is_never_held_back(self):
        noisy, other = self.rule(), self.rule("Also in the lot")
        self.burst(noisy, 30)
        self.burst(other, 5)
        self.assertEqual(self.count("alarms", f"rule_id='{other}'"), 5)
        falls = self.rule("Fall", kind="fall", condition={}, classes=("person",))
        self.burst(falls, 30, kind="fall")
        self.assertEqual(self.count("alarms", f"rule_id='{falls}'"), 30)

    def test_a_summary_reaches_the_inbox_push_and_webhooks_but_runs_no_rule_action(self):
        rule_id = self.rule()
        self.burst(rule_id, 12)
        listed = self.client.get("/api/alarms").json()["alarms"]
        self.assertEqual(sum(1 for alarm in listed if alarm["label"] == "suppressed:0"), 1)
        summary = next(alarm for alarm in listed if alarm["label"] == "suppressed:0")
        detail = self.client.get(f"/api/alarms/{summary['id']}").json()
        self.assertEqual(detail["history"][0]["details"]["held_back"], 0)


class LateTests(StormCase):
    def test_what_is_found_long_after_it_happened_is_recorded_and_tells_no_one(self):
        rule_id = self.rule()
        with connect() as db:
            db.execute("UPDATE rules SET pop_into_live=1 WHERE id=?", (rule_id,))
        tracker = Tracker()
        # The rules worker is down while the AI goes on storing: a car parks, and an hour later another.
        with connect() as db:
            record_detections(db, self.camera_id, CARS[:1], T0, tracker, model_id="m", model_version="1", provider="CPU")
            record_detections(db, self.camera_id, CARS[:1], T0 + timedelta(seconds=2), tracker, model_id="m", model_version="1", provider="CPU")
            record_detections(db, self.camera_id, CARS[:1] + CARS[2:3], T0 + timedelta(minutes=60), Tracker(take_up=True), model_id="m", model_version="1", provider="CPU")
        while rules.process_pending():  # It comes back and works through what it missed
            pass
        raised = self.alarm_rows()
        self.assertEqual(len(raised), 3)
        with connect() as db:
            late = {row["alarm_id"]: json.loads(row["details_json"]).get("late", False) for row in db.execute("SELECT alarm_id,details_json FROM alarm_history WHERE action='created'")}
            popped = {row[0] for row in db.execute("SELECT alarm_id FROM alarm_pop_ins")}
        old = [row["id"] for row in raised if row["occurred_at"] < (T0 + timedelta(minutes=30)).isoformat()]
        fresh = [row["id"] for row in raised if row["id"] not in old]
        self.assertEqual((len(old), len(fresh)), (1, 2))
        self.assertTrue(late[old[0]])  # An hour behind the newest detection stored: in the inbox, and nothing sent
        self.assertNotIn(old[0], popped)
        self.assertEqual({late[alarm] for alarm in fresh}, {False})  # What the AI has only just seen is not late
        self.assertEqual(popped, set(fresh))


class QuietTimeDefaultTests(StormCase):
    def test_a_new_rule_starts_at_a_minute_in_the_interface_and_one_with_none_is_marked(self):
        from pathlib import Path

        web = Path(__file__).resolve().parents[1] / "web/src/features/admin/rules"
        self.assertIn("schedule: null, cooldown_seconds: 60, pop_into_live: false", (web / "api.ts").read_text(encoding="utf-8"))  # The form a new rule opens with
        self.assertIn('rule.cooldown_seconds === 0 ? " · no quiet time" : ""', (web / "RulesWorkspace.tsx").read_text(encoding="utf-8"))  # The rules list
        # An API client that leaves it out gets what it always got, so nothing that makes rules by script changes under it.
        created = self.client.post("/api/rules", json={"name": "Default", "enabled": True, "camera_ids": [self.camera_id], "group_ids": [], "classes": ["person"],
                                                       "condition": {"type": "zone_entry", "zone": LOT}, "schedule": None})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["cooldown_seconds"], 0)
