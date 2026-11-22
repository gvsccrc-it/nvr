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
