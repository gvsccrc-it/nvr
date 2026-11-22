"""Detections of an object that is not moving (#512).

The first class pins what rules, Playback's overlay and Insights make of a fixed scene. It was written before detections were thinned and
passed on that code; it must go on passing, on the rows as stored and on the same rows once thinned.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from app import insights, rules
from app.store import connect

from tests.test_attribute_search import NOW, SearchCase

T0 = (NOW - timedelta(hours=3)).replace(second=0)
FRAME = 2  # Seconds between analysed frames (the default, 0.5 frames a second)
RIGHT_HALF = {"x": 0.5, "y": 0, "width": 0.5, "height": 1}


def jitter(index: int) -> float:
    """A standing object's box is never exactly where it was: a thousandth of the picture either way."""
    return 0.001 * ((index * 7) % 3 - 1)


def scene() -> dict[str, tuple[str, list[tuple[int, float, float]]]]:
    """Each object's label and its (second, centre x, centre y) in every analysed frame it is in."""
    walker = [(second, 0.10 + 0.025 * second, 0.5) for second in range(0, 22, FRAME)]  # In from the left; over the line at x=0.5 between 14 s and 16 s
    walker += [(second, 0.625 + jitter(second), 0.5 + jitter(second + 1)) for second in range(22, 622, FRAME)]  # Stands for ten minutes
    walker += [(second, 0.625 + 0.03 * (second - 620), 0.5) for second in range(622, 632, FRAME)]  # And leaves to the right
    guests = {name: [(second, x + jitter(second + offset), 0.4 + jitter(second)) for second in range(100, 402, FRAME)]
              for offset, (name, x) in enumerate((("guest one", 0.75), ("guest two", 0.85)))}
    car = [(second, 0.3 + jitter(second), 0.8) for second in range(0, 702, FRAME)]  # Parked the whole time
    return {"walker": ("person", walker), **{name: ("person", path) for name, path in guests.items()}, "car": ("vehicle", car)}


class SceneCase(SearchCase):
    def setUp(self):
        super().setUp()
        rules._followers.clear()
        self.rule_ids = {name: self.add_rule(name, kind, condition, classes) for name, kind, condition, classes in (
            ("Loitering", "loitering", {"zone": RIGHT_HALF, "dwell_seconds": 120}, ["person"]),
            ("Parked", "inactivity", {"still_seconds": 180}, ["vehicle"]),
            ("Crowded", "occupancy", {"limit": 2, "for_seconds": 60, "zone": None}, ["person"]),
            ("Entered", "zone_entry", {"zone": RIGHT_HALF}, ["person"]),
            ("Crossed", "line_crossing", {"line": {"x1": 0.5, "y1": 0, "x2": 0.5, "y2": 1}, "direction": "both"}, ["person"]))}
        self.tracks = {name: str(uuid.uuid4()) for name in scene()}
        self.names = {track: name for name, track in self.tracks.items()}

    def add_rule(self, name: str, kind: str, condition: dict, classes: list[str]) -> str:
        rule_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from) VALUES(?,?,?,?,?,?,?)",
                       (rule_id, name, json.dumps([self.d1]), kind, json.dumps(condition), json.dumps(classes), (T0 - timedelta(days=1)).isoformat()))
        return rule_id

    def frames(self) -> list[tuple[int, list[tuple[str, str, float, float]]]]:
        by_second: dict[int, list[tuple[str, str, float, float]]] = {}
        for name, (label, path) in scene().items():
            for second, x, y in path:
                by_second.setdefault(second, []).append((name, label, x, y))
        return sorted(by_second.items())

    def store_frame(self, db, second: int, objects: list[tuple[str, str, float, float]]) -> None:
        stamp = (T0 + timedelta(seconds=second)).isoformat()
        for name, label, x, y in objects:
            db.execute("INSERT OR IGNORE INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'m','1','CPU')",
                       (self.tracks[name], self.d1, label, stamp, stamp))
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                       "VALUES(?,?,?,?,0.9,?,?,0.1,0.2,'m','1','CPU')", (self.d1, self.tracks[name], stamp, label, x - 0.05, y - 0.1))

    def store_all(self) -> None:
        with connect() as db:
            for second, objects in self.frames():
                self.store_frame(db, second, objects)

    def run_live(self) -> list[tuple[str, str, int]]:
        """Store the scene a frame at a time, with the rules worker's pass after each, as the two workers run; the events raised."""
        for second, objects in self.frames():
            with connect() as db:
                self.store_frame(db, second, objects)
            while rules.process_pending():
                pass
        return self.events()

    def events(self) -> list[tuple[str, str, int]]:
        with connect() as db:
            rows = db.execute("SELECT rule_name,track_id,occurred_at FROM rule_events ORDER BY occurred_at,rule_name,track_id").fetchall()
        return sorted((row["rule_name"], self.names.get(row["track_id"], "everyone"), self.second(row["occurred_at"])) for row in rows)

    @staticmethod
    def second(stamp: str) -> int:
        return round((datetime.fromisoformat(stamp.replace("Z", "+00:00")) - T0).total_seconds())

    def replayed(self) -> list[tuple[str, str, int]]:
        """What test mode says each rule would have fired over the stored scene."""
        found = []
        with connect() as db:
            loaded = {rule.name: rule for rule in rules.load_rules(db)}
        for name, rule in loaded.items():
            for event in rules.replay(rule, T0 - timedelta(minutes=5), T0 + timedelta(minutes=30)):
                found.append((name, self.names.get(event["track_id"], "everyone"), self.second(event["occurred_at"])))
        return sorted(found)

    def boxes_at(self, second: float) -> dict[str, tuple[float, float]]:
        """What Playback draws at a moment: the boxes of the newest stored frame at or before it, held for as long as the interface holds one
        (two and a half frame gaps, web/src/features/playback/overlay.ts), each object by name with its box's corner."""
        start, end = T0 + timedelta(seconds=second - 60), T0 + timedelta(seconds=second + 60)
        response = self.admin.get("/api/playback/overlay", params={"camera_id": self.d1, "start": start.isoformat(), "end": end.isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        frames = response.json()["frames"]
        self.assertFalse(response.json()["truncated"])
        moment = T0 + timedelta(seconds=second)
        before = [frame for frame in frames if datetime.fromisoformat(frame["at"]) <= moment]
        if not before or (moment - datetime.fromisoformat(before[-1]["at"])).total_seconds() > 2.5 * FRAME:
            return {}
        return {self.names[box["track_id"]]: (box["box"]["x"], box["box"]["y"]) for box in before[-1]["boxes"]}

    def heat(self) -> tuple[int, int, int]:
        with connect() as db:
            report = insights.heatmap(db, self.d1, "Dock 1", T0 - timedelta(minutes=5), T0 + timedelta(minutes=30), now=NOW)
        return sum(sum(row) for row in report["cells"]), report["peak"], report["tracks"]

    def insight_totals(self) -> dict[str, int]:
        """Insights' count of each zone and line over the scene: how many moments, all buckets and kinds of object together."""
        with connect() as db:
            report = insights.counts(db, {self.d1: "Dock 1"}, T0 - timedelta(hours=1), T0 + timedelta(hours=2), "hour", now=NOW)
        return {subject["name"]: sum(sum(cell) for cell in report["counts"][subject["id"]]) for subject in report["subjects"] if subject["kind"] != "camera"}

    def counts(self) -> dict[str, int]:
        with connect() as db:
            return {self.names[row["id"]]: row["detection_count"] for row in db.execute("SELECT id,detection_count FROM ai_tracks")}


# What the scene comes to on the code as it was before thinning.
REPLAYED = [("Crossed", "walker", 16), ("Crowded", "everyone", 158), ("Entered", "guest one", 100), ("Entered", "guest two", 100), ("Entered", "walker", 16),
            ("Loitering", "guest one", 220), ("Loitering", "guest two", 220), ("Loitering", "walker", 136), ("Parked", "car", 180)]
# Live, a crowd that stays is reported again each minute (each pass looks at the last minute and the rule's shortest cooldown is a minute).
LIVE = sorted(REPLAYED + [("Crowded", "everyone", second) for second in (218, 278, 338, 398)])
COUNTS = {"walker": 316, "guest one": 151, "guest two": 151, "car": 351}
# Box corners (x, y) at moments of playback. A standing object's box is within a fiftieth of the picture of where it is drawn (MOVED).
DRAWN = {
    10: {"walker": (0.30, 0.4), "car": (0.25, 0.7)},
    61: {"walker": (0.575, 0.4), "car": (0.25, 0.7)},  # Between two frames: the one before is still shown
    250: {"walker": (0.575, 0.4), "guest one": (0.70, 0.3), "guest two": (0.80, 0.3), "car": (0.25, 0.7)},
    500: {"walker": (0.575, 0.4), "car": (0.25, 0.7)},
    626: {"walker": (0.755, 0.4), "car": (0.25, 0.7)},
    660: {"car": (0.25, 0.7)},
    720: {},  # Everything has gone
}
CLOSE = 0.021
INSIGHTS = {"Entered": 3, "Crossed": 1}  # Each person into the zone once; the walker over the line once


class PinnedBehaviourTests(SceneCase):
    def prepare(self) -> None:
        """Nothing here; the thinned variant below thins the stored scene first."""

    def assert_drawn(self) -> None:
        for second, expected in DRAWN.items():
            drawn = self.boxes_at(second)
            self.assertEqual(set(drawn), set(expected), second)
            for name, (x, y) in expected.items():
                self.assertAlmostEqual(drawn[name][0], x, delta=CLOSE, msg=f"{name} at {second} s")
                self.assertAlmostEqual(drawn[name][1], y, delta=CLOSE, msg=f"{name} at {second} s")

    def test_the_rules_fire_at_the_same_moments_as_the_scene_is_stored(self):
        self.assertEqual(self.run_live(), LIVE)

    def test_test_mode_finds_the_same_moments_in_the_stored_scene(self):
        self.store_all()
        self.prepare()
        self.assertEqual(self.replayed(), sorted(REPLAYED))

    def test_playback_draws_each_object_where_it_was(self):
        self.store_all()
        self.prepare()
        self.assert_drawn()

    def test_insights_and_the_tracks_count_every_analysed_frame(self):
        self.store_all()
        self.prepare()
        self.assertEqual(self.counts(), COUNTS)
        self.assertEqual(self.heat(), (sum(COUNTS.values()), 351, 4))
        self.assertEqual(self.insight_totals(), INSIGHTS)


def mark_evaluated() -> None:
    """As the rules worker leaves it once it has evaluated everything stored."""
    with connect() as db:
        db.execute("UPDATE rules_cursor SET last_detection_id=(SELECT COALESCE(MAX(id),0) FROM ai_detections) WHERE singleton_id=1")


def stored_rows() -> int:
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM ai_detections").fetchone()[0]


class ThinnedBehaviourTests(PinnedBehaviourTests):
    """The same scene, the same answers, once the still objects' rows have been thinned."""

    def prepare(self) -> None:
        from app import detection_thinning

        before = stored_rows()
        mark_evaluated()
        done = detection_thinning.thin_pending(NOW)
        self.assertEqual(done["looked"], before)
        self.assertEqual(stored_rows(), before - done["removed"])
        # The walker stands for ten minutes, the guests for five and the car for nearly twelve: 969 frames come down to under a fifth.
        self.assertLess(stored_rows(), before / 5)
        self.assertEqual(self.counts(), COUNTS)  # Every track still counts every analysed frame

    def test_the_rules_fire_at_the_same_moments_as_the_scene_is_stored(self):
        self.assertEqual(self.run_live(), LIVE)
        self.prepare()
        # The rules worker restarts and reads the thinned paths again from the start: nothing new fires, nothing is read over and over.
        rules._followers.clear()
        with connect() as db:
            self.store_frame(db, 704, [("car", "vehicle", 0.3, 0.8)])
        while rules.process_pending():
            pass
        self.assertEqual(self.events(), LIVE)
        with connect() as db:
            for follower in rules._followers.values():
                self.assertGreater(follower.count, 0)
            car = db.execute("SELECT detection_count FROM ai_tracks WHERE id=?", (self.tracks["car"],)).fetchone()[0]
        self.assertEqual(car, COUNTS["car"] + 1)


class ThinningTests(SceneCase):
    def still(self, name: str, seconds: range, x: float = 0.4, y: float = 0.4, label: str = "person") -> str:
        track = self.tracks.setdefault(name, str(uuid.uuid4()))
        self.names[track] = name
        with connect() as db:
            for second in seconds:
                self.store_frame(db, second, [(name, label, x + jitter(second), y)])
        return track

    def thin(self, now: datetime = NOW) -> dict[str, int]:
        from app import detection_thinning

        return detection_thinning.thin_pending(now)

    def rows(self, track: str) -> list[tuple[int, int]]:
        with connect() as db:
            return [(self.second(row["detected_at"]), row["stands_for"]) for row in db.execute("SELECT detected_at,stands_for FROM ai_detections WHERE track_id=? ORDER BY detected_at,id", (track,))]

    def test_an_object_that_does_not_move_for_an_hour_keeps_a_row_every_thirty_seconds(self):
        track = self.still("sitter", range(0, 3602, FRAME))  # 1,801 analysed frames
        mark_evaluated()
        self.assertEqual(self.thin(), {"looked": 1801, "removed": 1680})
        kept = self.rows(track)
        self.assertEqual(len(kept), 121)  # THE NUMBER: one every 30 seconds for an hour, and the last sighting
        self.assertEqual([second for second, _ in kept], list(range(0, 3601, 30)))
        self.assertEqual({frames for _, frames in kept[:-1]}, {15})  # Each stands for its own frame and the fourteen after it
        self.assertEqual(kept[-1], (3600, 1))
        self.assertEqual(self.counts()["sitter"], 1801)
        self.assertEqual(self.thin(), {"looked": 0, "removed": 0})  # Thinned once: never looked at again

    def test_only_frames_that_are_old_and_evaluated_are_thinned(self):
        from app import detection_thinning

        track = self.still("sitter", range(0, 600, FRAME))
        # Not evaluated by the rules worker yet: nothing is touched, however old.
        self.assertEqual(self.thin(), {"looked": 0, "removed": 0})
        mark_evaluated()
        # Younger than the horizon: nothing is touched. The horizon is ten minutes, or the longest any enabled rule looks back and a margin.
        with connect() as db:
            self.assertEqual(detection_thinning.horizon_seconds(db), 600)
            db.execute("UPDATE rules SET condition_json=? WHERE name='Parked'", (json.dumps({"still_seconds": 3600}),))
            self.assertEqual(detection_thinning.horizon_seconds(db), 3720)
            db.execute("UPDATE rules SET enabled=0 WHERE name='Parked'")
            self.assertEqual(detection_thinning.horizon_seconds(db), 600)
        self.assertEqual(self.thin(T0 + timedelta(seconds=599)), {"looked": 0, "removed": 0})
        # Ten minutes after the 100th second, the frames up to it are old enough; the rest wait.
        done = self.thin(T0 + timedelta(seconds=700))
        self.assertEqual(done["looked"], 50)
        self.assertEqual([second for second, _ in self.rows(track)][:6], [0, 30, 60, 90, 100, 102])

    def test_rows_stored_before_this_version_are_never_touched(self):
        track = self.still("old", range(0, 300, FRAME))
        with connect() as db:  # As the migration leaves it on an installation with detections: the cursor at the newest row
            db.execute("UPDATE detection_thinning SET last_id=(SELECT MAX(id) FROM ai_detections)")
        mark_evaluated()
        self.assertEqual(self.thin(), {"looked": 0, "removed": 0})
        self.assertEqual(len(self.rows(track)), 150)

    def test_movement_a_zone_and_a_line_each_keep_the_row_that_matters(self):
        # Creeps right by 0.006 of the picture every two seconds, from 0.450: over the rules' line and into their zone (x=0.5) between
        # the frames at 16 s (0.498) and 18 s (0.504).
        creeper = str(uuid.uuid4())
        self.tracks["creeper"], self.names[creeper] = creeper, "creeper"
        with connect() as db:
            for index in range(15):
                self.store_frame(db, index * FRAME, [("creeper", "person", 0.45 + 0.006 * index, 0.5)])
        mark_evaluated()
        self.thin()
        # 0: first. 8 and 16: each more than a fiftieth on from the last kept. 18: only 0.006 on, but inside the zone and across the line.
        # 26: moved again. 28: last.
        self.assertEqual([second for second, _ in self.rows(creeper)], [0, 8, 16, 18, 26, 28])
        self.assertEqual(self.replayed(), [("Crossed", "creeper", 18), ("Entered", "creeper", 18)])  # The very frame, as before thinning

    def test_a_box_that_comes_to_touch_a_privacy_mask_is_kept_so_it_is_not_drawn_where_it_last_was(self):
        with connect() as db:
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,updated_at) VALUES(?,?, 'none', ?)",
                       (self.d1, json.dumps([{"x": 0, "y": 0, "width": 0.2015, "height": 1}]), NOW.isoformat()))
        drifter = str(uuid.uuid4())
        self.tracks["drifter"], self.names[drifter] = drifter, "drifter"
        with connect() as db:  # A box whose left edge drifts from 0.215 down to 0.194: over the mask's edge at the sixth frame (10 s)
            for index in range(8):
                self.store_frame(db, index * FRAME, [("drifter", "person", 0.265 - 0.003 * index, 0.5)])
        mark_evaluated()
        self.thin()
        self.assertEqual([second for second, _ in self.rows(drifter)], [0, 10, 14])  # First; the first that touches the mask; last
        self.assertEqual(set(self.boxes_at(8)), {"drifter"})
        self.assertEqual(self.boxes_at(14), {})  # In the mask from the tenth second: the box before it is not held on through

    def test_deleting_a_thinned_row_takes_the_frames_it_stood_for_off_the_tracks_count(self):
        track = self.still("sitter", range(0, 120, FRAME))  # 60 frames
        mark_evaluated()
        self.thin()
        self.assertEqual(self.rows(track), [(0, 15), (30, 15), (60, 15), (90, 14), (118, 1)])
        with connect() as db:  # Retention removes the first minute's footage and its detections with it
            db.execute("DELETE FROM ai_detections WHERE track_id=? AND detected_at<?", (track, (T0 + timedelta(seconds=60)).isoformat()))
            count, first = db.execute("SELECT detection_count,first_seen_at FROM ai_tracks WHERE id=?", (track,)).fetchone()
        self.assertEqual((count, self.second(first)), (30, 60))

    def test_the_api_says_what_a_row_stands_for_and_names_the_model_from_the_track(self):
        from app.tracks import Tracker, record_detections

        at = T0 + timedelta(seconds=10)
        with connect() as db:
            (track,) = record_detections(db, self.d1, [{"label": "person", "confidence": 0.9, "box": {"x": 0.1, "y": 0.1, "width": 0.1, "height": 0.2}}], at, Tracker(),
                                         model_id="detector", model_version="7", provider="CPU")
            stored = db.execute("SELECT model_id,model_version,provider,stands_for FROM ai_detections WHERE track_id=?", (track,)).fetchone()
        self.assertEqual(tuple(stored), ("", "", "", 1))  # The model is the track's, stored once
        body = self.admin.get("/api/ai/detections", params={"camera_id": self.d1, "start": T0.isoformat(), "end": (T0 + timedelta(minutes=1)).isoformat()}).json()
        self.assertEqual([(item["model_id"], item["model_version"], item["provider"], item["stands_for"]) for item in body["detections"]], [("detector", "7", "CPU", 1)])
        one = self.admin.get(f"/api/ai/tracks/{track}").json()
        self.assertEqual([(item["model_id"], item["stands_for"]) for item in one["detections"]], [("detector", 1)])

    def test_with_nothing_to_thin_nothing_is_written(self):
        from app import detection_thinning

        self.still("sitter", range(0, 60, FRAME))
        mark_evaluated()
        self.thin()
        statements: list[str] = []
        real = detection_thinning.connect

        class Watched:
            def __enter__(self_inner):
                self_inner.context = real()
                db = self_inner.context.__enter__()
                db.set_trace_callback(statements.append)
                return db

            def __exit__(self_inner, *args):
                return self_inner.context.__exit__(*args)

        with unittest_patch(detection_thinning, "connect", Watched):
            self.assertEqual(self.thin(), {"looked": 0, "removed": 0})
        self.assertTrue(statements)
        self.assertFalse([statement for statement in statements if statement.lstrip().split()[0].upper() in {"BEGIN", "DELETE", "INSERT", "UPDATE"}], statements)


def unittest_patch(target, name, value):
    from unittest.mock import patch

    return patch.object(target, name, value)
