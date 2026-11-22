"""Occupancy rules (#254): more than a set number of objects in the frame or a zone, for a set time."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import occupancy, rules
from app.store import connect, initialize

START = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def seen(track: str, seconds: range, x: float = 0.5, y: float = 0.5, step: float = 0.5) -> list[tuple[str, datetime, float, float]]:
    return [(track, START + timedelta(seconds=second * step), x, y) for second in range(seconds.start, seconds.stop)]


class ExceedanceTests(unittest.TestCase):
    def moments(self, detections, **condition):
        return [at - START for at in occupancy.exceedances(sorted(detections, key=lambda item: item[1]), condition)]

    def test_fires_once_when_the_count_has_stayed_over_the_limit_for_the_set_time(self):
        crowd = seen("a", range(0, 80)) + seen("b", range(0, 80)) + seen("c", range(0, 80))  # three people for 40 s
        self.assertEqual(self.moments(crowd, limit=2, for_seconds=20), [timedelta(seconds=20)])

    def test_at_the_limit_is_not_over_it(self):
        self.assertEqual(self.moments(seen("a", range(0, 80)) + seen("b", range(0, 80)), limit=2, for_seconds=5), [])

    def test_a_brief_crowd_shorter_than_the_set_time_does_not_fire(self):
        crowd = seen("a", range(0, 80)) + seen("b", range(0, 80)) + seen("c", range(0, 12))  # a third person for 6 s
        self.assertEqual(self.moments(crowd, limit=2, for_seconds=20), [])

    def test_a_second_crowd_after_a_dip_fires_again(self):
        first = seen("a", range(0, 60)) + seen("b", range(0, 60)) + seen("c", range(0, 60))
        second = seen("a", range(200, 260)) + seen("b", range(200, 260)) + seen("c", range(200, 260))
        self.assertEqual(len(self.moments(first + second, limit=2, for_seconds=10)), 2)

    def test_with_a_zone_only_people_inside_it_count(self):
        zone = {"x": 0.6, "y": 0, "width": 0.4, "height": 1}
        inside = seen("a", range(0, 60), x=0.8) + seen("b", range(0, 60), x=0.7)
        outside = seen("c", range(0, 60), x=0.1) + seen("d", range(0, 60), x=0.2)
        self.assertEqual(self.moments(inside + outside, zone=zone, limit=2, for_seconds=5), [])
        self.assertEqual(len(self.moments(inside + outside + seen("e", range(0, 60), x=0.9), zone=zone, limit=2, for_seconds=5)), 1)

    def test_detections_every_second_still_count_as_present_between_them(self):
        crowd = [(name, START + timedelta(seconds=second), 0.5, 0.5) for name in "abc" for second in range(0, 40, 2)]
        self.assertEqual(len(self.moments(crowd, limit=2, for_seconds=10)), 1)


class OccupancyRuleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.directory.name) / "state.sqlite3")
        initialize()
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('cam','Dining room','x')")
            db.execute("INSERT INTO rules(id,name,condition_type,condition_json,classes_json,active_from) VALUES('crowded','Crowded','occupancy','{\"limit\":2,\"for_seconds\":10,\"zone\":null}','[\"person\"]','2026-11-01T00:00:00+00:00')")
            db.execute("UPDATE rules SET camera_ids_json='[\"cam\"]'")
            for name in "abc":
                db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'m','1','cpu')", (name, "cam", "person", START.isoformat(), (START + timedelta(seconds=40)).isoformat()))
                for second in range(0, 40):
                    db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) VALUES('cam',?,?,'person',0.9,0.4,0.4,0.1,0.2,'m','1','cpu')",
                               (name, (START + timedelta(seconds=second)).isoformat()))

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.directory.cleanup()

    def test_the_worker_raises_one_alarm_for_a_crowd_however_many_passes_see_it(self):
        while rules.process_pending(batch_size=25):
            pass
        rules.process_pending()
        with connect() as db:
            events = db.execute("SELECT label,condition_type,camera_id FROM rule_events").fetchall()
        self.assertEqual([tuple(row) for row in events], [("occupancy", "occupancy", "cam")])

    def test_test_mode_replays_the_same_crowd(self):
        with connect() as db:
            rule = rules.load_rules(db)[0]
        fired = rules.replay(rule, START - timedelta(minutes=1), START + timedelta(minutes=5))
        self.assertEqual([(event["camera_name"], event["label"]) for event in fired], [("Dining room", "occupancy")])

    def test_the_rules_table_still_rejects_unknown_conditions(self):
        with connect() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO rules(name,condition_type,condition_json,classes_json,active_from) VALUES('Bad','nonsense','{}','[]','2026-11-06T00:00:00+00:00')")


class OneAlarmAStretchTests(unittest.TestCase):
    """#513: a room over its limit raised an alarm a minute for as long as it stayed so. One stretch is one alarm, live as in test mode."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.directory.name) / "state.sqlite3")
        initialize()
        rules._counting.clear()
        self.addCleanup(rules._counting.clear)
        with connect() as db:
            for camera, name in (("cam", "Dining room"), ("other", "Corridor")):
                db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,'x')", (camera, name))
        self.rule(limit=2, for_seconds=10)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.directory.cleanup()

    def rule(self, **condition) -> None:
        import json

        with connect() as db:
            db.execute("DELETE FROM rules")
            db.execute("INSERT INTO rules(id,name,condition_type,condition_json,classes_json,active_from,camera_ids_json) VALUES('crowded','Crowded','occupancy',?,'[\"person\"]','2026-11-01T00:00:00+00:00','[\"cam\"]')",
                       (json.dumps({"zone": None, **condition}),))

    def frame(self, second: int, people: str, camera: str = "cam") -> None:
        """One analysed frame: who is in it. Stored, and then the rules worker's pass, as the two run."""
        stamp = (START + timedelta(seconds=second)).isoformat()
        with connect() as db:
            for name in people:
                track = f"{camera}-{name}"
                db.execute("INSERT OR IGNORE INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'person',?,?,'m','1','cpu')", (track, camera, stamp, stamp))
                db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) VALUES(?,?,?,'person',0.9,0.4,0.4,0.1,0.2,'','','')",
                           (camera, track, stamp))
        while rules.process_pending():
            pass

    def play(self, seconds: range, people: str, camera: str = "cam") -> None:
        for second in seconds:
            self.frame(second, people, camera)

    def events(self) -> list[tuple[int, int | None]]:
        """Each occupancy event: the second it was raised at and the second its stretch ended (None while it goes on)."""
        def second(stamp):
            return None if stamp is None else round((datetime.fromisoformat(stamp) - START).total_seconds())
        with connect() as db:
            return [(second(row["occurred_at"]), second(row["ended_at"])) for row in db.execute("SELECT occurred_at,ended_at FROM rule_events ORDER BY occurred_at")]

    def history(self) -> list[str]:
        with connect() as db:
            return [row[0] for row in db.execute("SELECT action FROM alarm_history ORDER BY id")]

    def replayed(self) -> int:
        with connect() as db:
            rule = rules.load_rules(db)[0]
        return len(rules.replay(rule, START - timedelta(minutes=1), START + timedelta(hours=2)))

    def test_ten_minutes_over_the_limit_is_one_alarm_as_test_mode_says(self):
        self.play(range(0, 600, 2), "abc")
        self.assertEqual(self.events(), [(10, None)])
        self.assertEqual(self.replayed(), 1)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM alarms").fetchone()[0], 1)

    def test_when_the_count_drops_back_the_alarm_records_it_and_a_second_stretch_is_a_second_alarm(self):
        self.play(range(0, 120, 2), "abc")
        self.play(range(120, 240, 2), "ab")  # One leaves
        self.assertEqual(self.events(), [(10, 120)])  # Counted as present for two seconds after the last sighting, as the rule counts
        self.assertEqual(self.history(), ["created", "ended"])
        self.play(range(240, 360, 2), "abc")  # And comes back
        self.assertEqual(self.events(), [(10, 120), (248, None)])  # Present from two seconds before the first sighting back
        self.assertEqual(self.replayed(), 2)
        with connect() as db:
            rule = rules.load_rules(db)[0]
        moments = [round((datetime.fromisoformat(event["occurred_at"]) - START).total_seconds()) for event in rules.replay(rule, START - timedelta(minutes=1), START + timedelta(hours=1))]
        self.assertEqual(moments, [10, 248])  # Test mode names the same two moments

    def test_everyone_leaving_at_once_ends_the_stretch_though_nothing_more_is_stored(self):
        from unittest.mock import patch

        self.play(range(0, 60, 2), "abc")
        self.assertEqual(self.events(), [(10, None)])
        real = rules.time.monotonic
        with patch.object(rules.time, "monotonic", lambda: real() + occupancy.END_QUIET + 1):  # Ten seconds with nothing stored for the camera
            self.assertEqual(rules.process_pending(), 0)
        self.assertEqual(self.events(), [(10, 58)])
        self.assertEqual(self.history(), ["created", "ended"])
        self.assertEqual(rules.process_pending(), 0)  # Said once
        self.assertEqual(self.history(), ["created", "ended"])

    def test_a_restart_in_the_middle_of_a_stretch_does_not_raise_it_again(self):
        self.play(range(0, 60, 2), "abc")
        rules._counting.clear()  # The rules worker restarts: what it remembered is gone
        self.play(range(60, 200, 2), "abc")
        self.assertEqual(self.events(), [(10, None)])
        # It restarts again, and this time the count dropped back while it was away and went over again.
        rules._counting.clear()
        with connect() as db:
            db.execute("UPDATE rules SET enabled=0")
        self.play(range(200, 260, 2), "ab")
        with connect() as db:
            db.execute("UPDATE rules SET enabled=1")
        self.play(range(260, 320, 2), "abc")
        raised = self.events()
        self.assertEqual(len(raised), 2)
        self.assertIsNotNone(raised[0][1])  # The first stretch is recorded as ended
        self.assertEqual(raised[1][1], None)

    def test_reminders_are_off_unless_asked_for_and_are_notes_on_the_one_alarm(self):
        self.play(range(0, 2400, 2), "abc")  # Forty minutes over the limit
        self.assertEqual((self.events(), self.history()), ([(10, None)], ["created"]))  # No reminder by default
        with connect() as db:
            db.execute("DELETE FROM alarm_history")
            db.execute("DELETE FROM alarms")
            db.execute("DELETE FROM rule_events")
            db.execute("DELETE FROM ai_detections")
        rules._counting.clear()
        self.rule(limit=2, for_seconds=10, remind_minutes=15)
        self.play(range(0, 2400, 2), "abc")
        self.assertEqual(self.events(), [(10, None)])  # Still one alarm
        self.assertEqual(self.history(), ["created", "reminded", "reminded"])  # At 15 and 30 minutes
        with connect() as db:
            import json

            minutes = [json.loads(row[0])["minutes"] for row in db.execute("SELECT details_json FROM alarm_history WHERE action='reminded' ORDER BY id")]
            self.assertEqual(db.execute("SELECT COUNT(*) FROM alarms").fetchone()[0], 1)
        self.assertEqual(minutes, [15, 30])
        self.assertEqual(occupancy.Stretch({"limit": 1, "for_seconds": 1, "remind_minutes": 1}).remind, 15 * 60)  # Never more often than every 15 minutes

    def test_a_camera_with_nothing_new_is_not_read(self):
        self.play(range(0, 60, 2), "abc")
        statements: list[str] = []
        real = rules.connect

        class Watched:
            def __enter__(self_inner):
                self_inner.context = real()
                db = self_inner.context.__enter__()
                db.set_trace_callback(statements.append)
                return db

            def __exit__(self_inner, *args):
                return self_inner.context.__exit__(*args)

        from unittest.mock import patch

        with patch.object(rules, "connect", Watched):
            self.frame(60, "xy", camera="other")  # Something on another camera
        about_detections = [statement for statement in statements if "FROM ai_detections" in statement and "camera_id='cam'" in statement]
        self.assertEqual(about_detections, [])
        # And a pass for the camera itself reads what arrived since the last one, not the rule's whole set time.
        self.rule(limit=2, for_seconds=3600)
        self.play(range(62, 200, 2), "abc")
        statements.clear()
        with patch.object(rules, "connect", Watched):
            self.frame(200, "abc")
        reads = [statement for statement in statements if "FROM ai_detections" in statement and "camera_id='cam'" in statement]
        self.assertEqual(len(reads), 1)
        self.assertIn((START + timedelta(seconds=198)).isoformat(), reads[0])  # From the last frame it was fed, on

    def test_the_reminder_interval_is_at_least_fifteen_minutes(self):
        from app.api.rules import Occupancy
        from pydantic import ValidationError

        self.assertIsNone(Occupancy(type="occupancy", limit=2).remind_minutes)
        self.assertEqual(Occupancy(type="occupancy", limit=2, remind_minutes=15).remind_minutes, 15)
        with self.assertRaises(ValidationError):
            Occupancy(type="occupancy", limit=2, remind_minutes=5)


if __name__ == "__main__":
    unittest.main()
