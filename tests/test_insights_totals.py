"""Insights and heatmap totals (#515): the same answers as adding up the rows, kept current through every write, resumable, idle without the
write lock, and fast over a month of a busy camera.

``reference_counts`` and ``reference_heatmap`` are the readers as they were before the totals (the version of #588), kept here verbatim
in what they compute, so every answer is compared with what the server used to say."""
from __future__ import annotations

import bisect
import json
import random
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import detection_thinning, insight_totals, insights, track_privacy, tracks
from app.clock import parse as _parse
from app.store import connect
from tests.test_attribute_search import SearchCase

BASE = datetime(2026, 3, 27, 0, 0, tzinfo=timezone.utc)  # Europe's clocks go forward on the 29th
DAYS = 4
LATER = BASE + timedelta(days=30)  # "Now" for working the totals out: every generated hour has settled
LABELS_STORED = ("person", "vehicle", "bicycle")  # a bicycle is on the heatmap, not in the counts


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


# --- the readers before the totals ---------------------------------------------------------------------------------------------------------


def reference_counts(db: sqlite3.Connection, cameras: dict[str, str], start: datetime, end: datetime, unit: str, *, label: str | None = None) -> dict:
    zone = insights.site_zone(db)
    spans = insights.buckets(start, end, zone, unit)
    starts = [span[0] for span in spans]
    labels = [label] if label else list(insights.LABELS)

    def index(moment: datetime) -> int | None:
        position = bisect.bisect_right(starts, moment) - 1
        return position if 0 <= position < len(spans) and moment < spans[position][1] and start <= moment < end else None

    series: dict[str, list[list[int]]] = {}
    ids = list(cameras)
    defined: dict[str, list] = defaultdict(list)
    for camera_id in ids:
        series[f"camera:{camera_id}"] = [[0] * len(insights.LABELS) for _ in spans]
    for entry in insights.definitions(db, ids):
        rule, camera_id = entry["rule"], str(entry["camera_id"])
        kind = "zone" if rule.condition_type == "zone_entry" else "line"
        series[f"{kind}:{rule.id}:{camera_id}"] = [[0] * len(insights.LABELS) for _ in spans]
        defined[camera_id].append((rule, series[f"{kind}:{rule.id}:{camera_id}"]))
    zones = track_privacy.zones_by_camera(db, ids)
    permitted, permitted_values = track_privacy.detection_visible_sql(zones, "d")
    for camera_id in ids:
        visible, visible_values = track_privacy.track_visible_sql(zones, alias="t", window=(_iso(start), _iso(end)))
        own = series[f"camera:{camera_id}"]
        watched = set().union(*(rule.classes for rule, _ in defined[camera_id])) if defined[camera_id] else set()
        cursor = db.execute(f"SELECT t.id,t.label,t.first_seen_at FROM ai_tracks t WHERE t.camera_id=? AND t.last_seen_at>=? AND t.first_seen_at<? AND {visible} ORDER BY t.first_seen_at",
                            (camera_id, _iso(start), _iso(end), *visible_values))
        while batch := cursor.fetchmany(insights.CHUNK):
            chosen = [row for row in batch if row["label"] in labels]
            for row in chosen:
                position = index(_parse(row["first_seen_at"]))
                if position is not None:
                    own[position][insights.LABELS.index(row["label"])] += 1
            label_of = {row["id"]: row["label"] for row in chosen if row["label"] in watched}
            if label_of:
                insights._count_moments(db, list(label_of), label_of, defined[camera_id], index, permitted, permitted_values)
    return series


def reference_heatmap(db: sqlite3.Connection, camera_id: str, start: datetime, end: datetime, *, label: str | None = None) -> tuple[list[list[int]], int]:
    clauses, values = ["d.camera_id=?", "d.detected_at>=?", "d.detected_at<?"], [camera_id, _iso(start), _iso(end)]
    if label:
        clauses.append("t.label=?")
        values.append(label)
    permitted, permitted_values = track_privacy.detection_visible_sql(track_privacy.zones_by_camera(db, [camera_id]), "d")
    clauses.append(permitted)
    values.extend(permitted_values)
    cells = [[0] * insights.HEAT_COLUMNS for _ in range(insights.HEAT_ROWS)]
    for row in db.execute(
        f"SELECT min(CAST((d.box_x+d.box_width/2)*{insights.HEAT_COLUMNS} AS INTEGER),{insights.HEAT_COLUMNS - 1}) AS column,"
        f"min(CAST((d.box_y+d.box_height/2)*{insights.HEAT_ROWS} AS INTEGER),{insights.HEAT_ROWS - 1}) AS row,SUM(d.stands_for) AS samples "
        f"FROM ai_detections d JOIN ai_tracks t ON t.id=d.track_id WHERE {' AND '.join(clauses)} GROUP BY column,row", values):
        cells[row["row"]][row["column"]] = row["samples"]
    count = db.execute(f"SELECT COUNT(DISTINCT d.track_id) FROM ai_detections d JOIN ai_tracks t ON t.id=d.track_id WHERE {' AND '.join(clauses)}", values).fetchone()[0]
    return cells, count


# --- generated activity ----------------------------------------------------------------------------------------------------------------------


def generate(camera_ids: list[str], *, seed: int, tracks_per_camera: int, start: datetime = BASE, days: int = DAYS) -> None:
    """Tracks of every length (a few seconds to several hours, so many cross an hour or a day), wandering over the picture, at any moment."""
    chance = random.Random(seed)
    span = days * 86400
    with connect() as db:
        for camera_id in camera_ids:
            track_rows, detection_rows = [], []
            for _ in range(tracks_per_camera):
                track_id = str(uuid.uuid4())
                label = chance.choice(LABELS_STORED)
                began = start + timedelta(seconds=chance.uniform(0, span), microseconds=chance.choice((0, 1, 500_000)))
                points = chance.randint(1, 14)
                lasting = chance.choice((5, 60, 600, 3600, 3 * 3600))
                x, y = chance.random(), chance.random()
                track_rows.append((track_id, camera_id, label, _iso(began), _iso(began)))
                for step in range(points):
                    x = min(0.9, max(0.0, x + chance.uniform(-0.3, 0.3)))
                    y = min(0.85, max(0.0, y + chance.uniform(-0.3, 0.3)))
                    at = began + timedelta(seconds=lasting * step / max(1, points - 1))
                    detection_rows.append((camera_id, track_id, _iso(at), label, x, y, 0.1, 0.15, chance.choice((1, 1, 1, 3))))
            db.executemany("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,?,?,?,'m','1','CPU')", track_rows)
            db.executemany("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider,stands_for) "
                           "VALUES(?,?,?,?,0.9,?,?,?,?,'m','1','CPU',?)", detection_rows)


class TotalsCase(SearchCase):
    def setUp(self):
        super().setUp()
        self.zone_rule = self.add_rule("Right half", [self.d1, self.d2], "zone_entry", {"zone": {"x": 0.5, "y": 0, "width": 0.5, "height": 1}}, ["person", "vehicle"])
        self.line_rule = self.add_rule("Gate line", [self.d1], "line_crossing", {"line": {"x1": 0.5, "y1": 0, "x2": 0.5, "y2": 1}, "direction": "both"}, ["person"])
        self.set_mask(self.d2, [{"x": 0.0, "y": 0.0, "width": 0.3, "height": 0.4}])

    def add_rule(self, name: str, camera_ids: list[str], kind: str, condition: dict, classes: list[str]) -> str:
        rule_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO rules(id,name,camera_ids_json,condition_type,condition_json,classes_json,active_from) VALUES(?,?,?,?,?,?,?)",
                       (rule_id, name, json.dumps(camera_ids), kind, json.dumps(condition), json.dumps(classes), (BASE - timedelta(days=30)).isoformat()))
        return rule_id

    def set_mask(self, camera_id: str, zones: list[dict]) -> None:
        with connect() as db:
            db.execute("INSERT INTO privacy_masks(camera_id,zones_json,camera_side,updated_at) VALUES(?,?,'none',?) "
                       "ON CONFLICT(camera_id) DO UPDATE SET zones_json=excluded.zones_json", (camera_id, json.dumps(zones), BASE.isoformat()))

    def set_time_zone(self, name: str) -> None:
        with connect() as db:
            db.execute("UPDATE site_display_settings SET time_zone=? WHERE singleton_id=1", (name,))

    def current_hours(self, camera_id: str) -> set[int]:
        with connect() as db:
            return {row[0] for row in db.execute("SELECT hour FROM insight_fresh WHERE camera_id=? AND state=1", (camera_id,))}

    def assert_same(self, start: datetime, end: datetime, unit: str = "hour", label: str | None = None, camera_ids: list[str] | None = None) -> None:
        cameras = {camera_id: self.names[camera_id] for camera_id in (camera_ids or [self.d1, self.d2, self.y1])}
        with connect() as db:
            expected = reference_counts(db, cameras, start, end, unit, label=label)
            got = insights.counts(db, cameras, start, end, unit, label=label, now=LATER)["counts"]
            self.assertEqual(got, expected, (start, end, unit, label))
            for camera_id in cameras:
                body = insights.heatmap(db, camera_id, cameras[camera_id], start, end, label=label, now=LATER)
                self.assertEqual((body["cells"], body["tracks"]), reference_heatmap(db, camera_id, start, end, label=label), (camera_id, start, end, label))

    def ranges(self):
        """Whole hours, ragged ends (to the microsecond), a single quarter hour, and the whole generated stretch, by hour and by day."""
        yield BASE, BASE + timedelta(days=DAYS), "day"
        yield BASE + timedelta(hours=1), BASE + timedelta(hours=30), "hour"
        yield BASE + timedelta(hours=2, minutes=7, seconds=13, microseconds=500_000), BASE + timedelta(days=2, hours=5, minutes=52, microseconds=1), "hour"
        yield BASE + timedelta(hours=40, minutes=15), BASE + timedelta(hours=40, minutes=30), "hour"
        yield BASE + timedelta(hours=11, minutes=59, seconds=59), BASE + timedelta(days=3, hours=12, seconds=1), "day"


class SameAnswerTests(TotalsCase):
    def setUp(self):
        super().setUp()
        generate([self.d1, self.d2, self.y1], seed=515, tracks_per_camera=500)

    def test_every_range_label_and_time_zone_gives_the_old_answer_before_and_after_the_totals(self):
        for zone_name in ("UTC", "Europe/London", "Asia/Kolkata", "Asia/Kathmandu", "America/St_Johns"):
            self.set_time_zone(zone_name)
            for start, end, unit in self.ranges():
                for label in (None, "person", "vehicle"):
                    with self.subTest(zone=zone_name, start=start, unit=unit, label=label, totals=False):
                        self.assert_same(start, end, unit, label)
        done = insight_totals.catch_up(LATER)
        self.assertGreater(done["hours"], DAYS * 24)
        self.assertTrue(self.current_hours(self.d1) and self.current_hours(self.d2))
        with connect() as db:  # The generated activity reaches what the totals must get right: tracks wholly or partly behind the mask, zones and lines
            self.assertGreater(db.execute("SELECT COUNT(*) FROM insight_begins WHERE camera_id=? AND visible_at IS NULL", (self.d2,)).fetchone()[0], 0)
            self.assertGreater(db.execute("SELECT COUNT(*) FROM insight_begins b JOIN ai_tracks t ON t.id=b.track_id WHERE b.camera_id=? AND b.visible_at>t.first_seen_at",
                                          (self.d2,)).fetchone()[0], 0)
            self.assertGreater(db.execute("SELECT COUNT(DISTINCT rule_id) FROM insight_events WHERE camera_id=?", (self.d1,)).fetchone()[0], 1)
        for zone_name in ("UTC", "Europe/London", "Asia/Kolkata", "Asia/Kathmandu", "America/St_Johns"):
            self.set_time_zone(zone_name)
            for start, end, unit in self.ranges():
                for label in (None, "person", "vehicle", "bicycle"):
                    with self.subTest(zone=zone_name, start=start, unit=unit, label=label, totals=True):
                        if label != "bicycle":
                            self.assert_same(start, end, unit, label)
                        else:
                            with connect() as db:
                                body = insights.heatmap(db, self.d1, "Dock 1", start, end, label=label, now=LATER)
                                self.assertEqual((body["cells"], body["tracks"]), reference_heatmap(db, self.d1, start, end, label=label))

    def test_the_totals_are_what_is_read_for_whole_hours(self):
        insight_totals.catch_up(LATER)
        start, end = BASE + timedelta(hours=2, minutes=7), BASE + timedelta(days=2, hours=5, minutes=52)
        parts: list = []
        real = insights._count_rows

        def spy(db, camera_id, given, *args, **kwargs):
            parts.append(given)
            return real(db, camera_id, given, *args, **kwargs)

        with connect() as db, patch.object(insights, "_count_rows", spy):
            insights.counts(db, {self.d1: "Dock 1"}, start, end, "hour", now=LATER)
        # Only the ragged ends are read from the rows: 08 minutes at the start and 07 at the end
        self.assertEqual(parts, [[(start, BASE + timedelta(hours=2, minutes=15)), (BASE + timedelta(days=2, hours=5, minutes=45), end)]])
        with connect() as db:  # The heatmap's whole hours come from its totals too: spoil them and its answer changes by exactly that much
            spoiled = db.execute("SELECT COUNT(*) FROM insight_heat WHERE camera_id=? AND hour>=? AND hour<?",
                                 (self.d1, insight_totals.floor_units(start, 3600) + 1, insight_totals.floor_units(end, 3600))).fetchone()[0]
            db.execute("UPDATE insight_heat SET samples=samples+1 WHERE camera_id=?", (self.d1,))
            body = insights.heatmap(db, self.d1, "Dock 1", start, end, now=LATER)
            cells, _ = reference_heatmap(db, self.d1, start, end)
        self.assertGreater(spoiled, 0)
        self.assertEqual(sum(map(sum, body["cells"])) - sum(map(sum, cells)), spoiled)

    def test_partly_worked_out_totals_give_the_same_answer(self):
        insight_totals.catch_up(BASE + timedelta(days=2))  # Only the first day or so has settled
        self.assertTrue(self.current_hours(self.d1))
        self.assertLess(max(self.current_hours(self.d1)), insight_totals.floor_units(BASE + timedelta(days=2), 3600))
        for start, end, unit in self.ranges():
            with self.subTest(start=start, unit=unit):
                self.assert_same(start, end, unit)

    def test_an_operator_limited_to_some_cameras_gets_the_old_answer_for_those_alone(self):
        insight_totals.catch_up(LATER)
        start, end = BASE + timedelta(hours=3, minutes=20), BASE + timedelta(days=2)
        body = self.limited.get("/api/insights", params={"start": start.isoformat(), "end": end.isoformat(), "camera_ids": [self.d1, self.d2, self.y1]}).json()
        self.assertEqual({subject["camera_id"] for subject in body["subjects"]}, {self.d1, self.d2})  # Yard 1 is not theirs
        with connect() as db:
            self.assertEqual(body["counts"], reference_counts(db, {self.d1: "Dock 1", self.d2: "Dock 2"}, start, end, "hour"))

    def test_a_changed_mask_or_rule_is_not_answered_from_totals_worked_out_before_it(self):
        insight_totals.catch_up(LATER)
        self.set_mask(self.d2, [{"x": 0.4, "y": 0.4, "width": 0.4, "height": 0.4}])
        self.set_mask(self.d1, [{"x": 0.0, "y": 0.5, "width": 0.2, "height": 0.5}])
        with connect() as db:
            db.execute("UPDATE rules SET condition_json=? WHERE id=?", (json.dumps({"zone": {"x": 0.2, "y": 0, "width": 0.3, "height": 1}}), self.zone_rule))
        for start, end, unit in self.ranges():
            with self.subTest(start=start, unit=unit, worked_out=False):
                self.assert_same(start, end, unit)
        self.assertGreaterEqual(insight_totals.step(LATER)["restarted"], 2)
        insight_totals.catch_up(LATER)
        for start, end, unit in self.ranges():
            with self.subTest(start=start, unit=unit, worked_out=True):
                self.assert_same(start, end, unit)


class UpkeepTests(TotalsCase):
    def setUp(self):
        super().setUp()
        generate([self.d1, self.d2], seed=7, tracks_per_camera=300)
        insight_totals.catch_up(LATER)
        self.everything = (BASE - timedelta(hours=1), BASE + timedelta(days=DAYS, hours=4), "hour")

    def check(self):
        for start, end, unit in (*self.ranges(), self.everything):
            self.assert_same(start, end, unit)
        insight_totals.catch_up(LATER)
        for start, end, unit in (*self.ranges(), self.everything):
            self.assert_same(start, end, unit)

    def hour(self, moment: datetime) -> int:
        return insight_totals.floor_units(moment, 3600)

    def test_a_detection_added_in_the_middle_of_a_track_takes_the_mark_off_its_hour_to_the_track_end(self):
        with connect() as db:
            track = db.execute("SELECT id,first_seen_at,last_seen_at FROM ai_tracks WHERE camera_id=? AND label='person' "
                               "AND julianday(last_seen_at)-julianday(first_seen_at)>0.1 ORDER BY first_seen_at LIMIT 1", (self.d1,)).fetchone()
            middle = _parse(track["first_seen_at"]) + (_parse(track["last_seen_at"]) - _parse(track["first_seen_at"])) / 3
            before = self.current_hours(self.d1)
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                       "VALUES(?,?,?,'person',0.9,0.7,0.3,0.1,0.1,'m','1','CPU')", (self.d1, track["id"], _iso(middle)))
        gone = before - self.current_hours(self.d1)
        self.assertEqual(gone, set(range(self.hour(middle), self.hour(_parse(track["last_seen_at"])) + 1)) & before)
        self.check()

    def test_a_detection_added_after_the_last_of_its_track_takes_the_mark_off_its_own_hour_only(self):
        with connect() as db:
            track = db.execute("SELECT id,last_seen_at FROM ai_tracks WHERE camera_id=? ORDER BY first_seen_at LIMIT 1", (self.d1,)).fetchone()
            later = _parse(track["last_seen_at"]) + timedelta(hours=5)
            before = self.current_hours(self.d1)
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                       "VALUES(?,?,?,'person',0.9,0.7,0.3,0.1,0.1,'m','1','CPU')", (self.d1, track["id"], _iso(later)))
        self.assertEqual(before - self.current_hours(self.d1), {self.hour(later)})
        self.check()

    def test_a_first_unmasked_detection_marks_the_hour_its_track_began(self):
        with connect() as db:
            track_id = str(uuid.uuid4())
            began = BASE + timedelta(hours=5, minutes=50)
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'person',?,?,'m','1','CPU')",
                       (track_id, self.d2, _iso(began), _iso(began)))
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                       "VALUES(?,?,?,'person',0.9,0.05,0.05,0.1,0.1,'m','1','CPU')", (self.d2, track_id, _iso(began)))  # Behind the mask
        insight_totals.catch_up(LATER)
        with connect() as db:
            self.assertIsNone(db.execute("SELECT visible_at FROM insight_begins WHERE track_id=?", (track_id,)).fetchone()[0])
            begun = self.current_hours(self.d2)
            self.assertIn(self.hour(began), begun)
            seen = began + timedelta(hours=3)  # Out from behind it three hours later: it now counts in the hour it began, for a range that reaches this
            db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                       "VALUES(?,?,?,'person',0.9,0.7,0.7,0.1,0.1,'m','1','CPU')", (self.d2, track_id, _iso(seen)))
        self.assertEqual(begun - self.current_hours(self.d2), {self.hour(began), self.hour(seen)})
        self.check()
        for end in (seen, seen + timedelta(microseconds=1), seen + timedelta(minutes=10)):  # The range ends just before, at and after it is seen
            with self.subTest(end=end):
                self.assert_same(began - timedelta(minutes=50), end, "hour")
                self.assert_same(began - timedelta(minutes=50), end + timedelta(days=1), "day")

    def test_retention_removing_the_oldest_detections(self):
        with connect() as db:
            removed = tracks.purge_expired_detections(db, self.d1, BASE + timedelta(days=1, hours=3, minutes=17))
        self.assertGreater(removed, 0)
        self.check()

    def test_thinning(self):
        with connect() as db:
            db.execute("UPDATE detection_thinning SET last_id=0 WHERE singleton_id=1")
            db.execute("INSERT INTO rules_cursor(singleton_id,last_detection_id) VALUES(1,(SELECT MAX(id) FROM ai_detections)) "
                       "ON CONFLICT(singleton_id) DO UPDATE SET last_detection_id=excluded.last_detection_id")
        thinned = 0
        while (done := detection_thinning.thin_step(LATER))["looked"]:
            thinned += done["removed"]
        self.assertGreater(thinned, 0)
        self.check()

    def test_a_track_removed_with_its_footage_or_relabelled(self):
        with connect() as db:
            gone = [row[0] for row in db.execute("SELECT id FROM ai_tracks WHERE camera_id=? ORDER BY first_seen_at LIMIT 40", (self.d1,))]
            db.execute(f"DELETE FROM ai_tracks WHERE id IN ({','.join('?' * len(gone))})", gone)
            db.execute("UPDATE ai_tracks SET label='vehicle' WHERE id IN (SELECT id FROM ai_tracks WHERE camera_id=? AND label='person' ORDER BY last_seen_at DESC LIMIT 30)", (self.d2,))
        self.check()

    def test_a_camera_removed_takes_its_totals_with_it(self):
        with connect() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("DELETE FROM cameras WHERE id=?", (self.d2,))
        insight_totals.step(LATER)
        with connect() as db:
            for table in ("insight_state", "insight_fresh", "insight_begins", "insight_events", "insight_heat", "insight_heat_tracks", "insight_masks"):
                self.assertIsNone(db.execute(f"SELECT 1 FROM {table} WHERE camera_id=?", (self.d2,)).fetchone(), table)


class BackfillTests(TotalsCase):
    def setUp(self):
        super().setUp()
        generate([self.d1, self.d2], seed=11, tracks_per_camera=200)

    def test_a_backfill_stopped_part_way_goes_on_where_it_stopped_without_working_anything_out_twice(self):
        worked: list[tuple[str, int]] = []
        stop = threading.Event()
        real = insight_totals.work_out_hour

        def counted(db, camera_id, hour, *args):
            worked.append((camera_id, hour))
            if len(worked) == 10:
                stop.set()  # The worker is stopping
            return real(db, camera_id, hour, *args)

        with patch.object(insight_totals, "work_out_hour", counted):
            first = insight_totals.step(LATER, stop=stop)
            self.assertEqual(first["hours"], 10)
            self.assertGreater(first["waiting"], 0)
            hours = sorted(hour for _, hour in worked)
            self.assertEqual(hours[-1], max(hours))  # Newest first: what is asked for most is ready first
            for start, end, unit in self.ranges():
                self.assert_same(start, end, unit)  # Part done: the same answers
            insight_totals.catch_up(LATER)
        self.assertEqual(len(worked), len(set(worked)))
        with connect() as db:
            first_hour = insight_totals.floor_units(_parse(db.execute("SELECT MIN(first_seen_at) FROM ai_tracks WHERE camera_id=?", (self.d1,)).fetchone()[0]), 3600)
            last_hour = insight_totals.settled_hour(db, LATER)
        self.assertEqual(self.current_hours(self.d1), set(range(first_hour, last_hour + 1)))

    def test_an_hour_written_to_while_it_is_worked_out_is_not_marked_current(self):
        real = insight_totals.work_out

        def interrupted(db, camera_id, hour, *args):
            totals = real(db, camera_id, hour, *args)
            with connect() as other:  # The AI worker stores a detection in that hour meanwhile
                other.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'person',?,?,'m','1','CPU')",
                              (f"late-{hour}", camera_id, _iso(insight_totals.unit_start(hour, 3600)), _iso(insight_totals.unit_start(hour, 3600))))
            return totals

        with connect() as db, patch.object(insight_totals, "work_out", interrupted):
            hour = insight_totals.floor_units(BASE + timedelta(hours=10), 3600)
            prints, defined, zones = insight_totals.fingerprints(db, [self.d1])
            insight_totals._restart(db, self.d1, prints[self.d1], zones.get(self.d1, []))
            self.assertFalse(insight_totals.work_out_hour(db, self.d1, hour, prints[self.d1], defined[self.d1], zones))
        self.assertNotIn(hour, self.current_hours(self.d1))
        insight_totals.catch_up(LATER)
        self.assertIn(hour, self.current_hours(self.d1))
        self.assert_same(BASE, BASE + timedelta(days=1), "hour")

    def test_an_idle_pass_takes_no_write_lock(self):
        insight_totals.catch_up(LATER)
        with connect() as db:
            before = db.execute("PRAGMA data_version").fetchone()[0], db.total_changes
        holder = sqlite3.connect(str(self.environment["NVR_DB_PATH"]), timeout=5, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")  # Another process is writing: a pass that wanted the lock would wait for it
        try:
            started = time.monotonic()
            done = insight_totals.step(LATER)
            took = time.monotonic() - started
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(done, {"hours": 0, "waiting": 0, "restarted": 0})
        self.assertLess(took, 1.0)
        with connect() as db:
            self.assertEqual(db.execute("PRAGMA data_version").fetchone()[0], before[0])

    def test_a_salvage_forgets_the_totals_so_they_are_worked_out_again_from_the_saved_rows(self):
        from app import db_salvage

        insight_totals.catch_up(LATER)
        with connect() as db:
            self.assertTrue(db.execute("SELECT 1 FROM insight_begins").fetchone())
            db_salvage._work_out_derived(db)
            for table in ("insight_state", "insight_fresh", "insight_begins", "insight_events", "insight_heat", "insight_heat_tracks", "insight_masks"):
                self.assertIsNone(db.execute(f"SELECT 1 FROM {table}").fetchone(), table)
        insight_totals.catch_up(LATER)
        self.assert_same(BASE, BASE + timedelta(days=DAYS), "day")

    def test_an_upgrade_works_nothing_out_at_start_up(self):
        # The migration only makes the tables and triggers: an hour is worked out by the background, never by the upgrade
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM insight_fresh").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM insight_begins").fetchone()[0], 0)


# --- a month of a busy camera ----------------------------------------------------------------------------------------------------------------

MONTH = 31
DETECTIONS = 10_000_000  # The ticket's camera
TRACKS = 300_000  # About 33 detections a track
BOUND_SECONDS = 1.0  # The stated bound for 31 days of Insights (by the hour) or of the heatmap, on this camera, on CI's runners


class MonthTests(TotalsCase):
    def fill_month(self, camera_id: str, start: datetime) -> None:
        """The totals a month of 10 million detections leaves: written directly, since storing the detections themselves would take minutes."""
        chance = random.Random(31)
        first_hour = insight_totals.floor_units(start, 3600)
        hours = MONTH * 24
        with connect() as db:
            prints, _, _ = insight_totals.fingerprints(db, [camera_id])
            db.execute("INSERT INTO insight_state(camera_id,fingerprint) VALUES(?,?)", (camera_id, prints[camera_id]))
            db.execute("BEGIN")
            db.executemany("INSERT INTO insight_fresh(camera_id,hour,state) VALUES(?,?,1)", ((camera_id, first_hour + hour) for hour in range(hours)))
            per_hour = TRACKS // hours
            db.executemany("INSERT INTO insight_begins(camera_id,slot,track_id,label,visible_at) VALUES(?,?,?,?,'')",
                           ((camera_id, (first_hour + hour) * 4 + chance.randrange(4), f"t{hour}-{n}", chance.choice(LABELS_STORED))
                            for hour in range(hours) for n in range(per_hour)))
            rule_ids = [self.zone_rule, self.line_rule]
            db.executemany("INSERT INTO insight_events(camera_id,slot,rule_id,label,n) VALUES(?,?,?,?,?)",
                           ((camera_id, slot, rule_id, label, chance.randint(1, 40)) for slot in range(first_hour * 4, (first_hour + hours) * 4)
                            for rule_id in rule_ids for label in ("person", "vehicle")))
            # About 200 cells of the 576 visited in an hour, for each label
            db.executemany("INSERT INTO insight_heat(camera_id,hour,label,cell,samples) VALUES(?,?,?,?,?)",
                           ((camera_id, first_hour + hour, label, cell, chance.randint(1, 200)) for hour in range(hours) for label in LABELS_STORED
                            for cell in chance.sample(range(insights.HEAT_COLUMNS * insights.HEAT_ROWS), 200)))
            db.executemany("INSERT INTO insight_heat_tracks(camera_id,hour,label,track_id) VALUES(?,?,?,?)",
                           ((camera_id, first_hour + hour + extra, chance.choice(LABELS_STORED), f"t{hour}-{n}") for hour in range(hours) for n in range(per_hour)
                            for extra in ((0, 1) if n % 5 == 0 else (0,))))  # A fifth of the tracks go on into the next hour
            db.execute("COMMIT")

    def test_a_month_of_ten_million_detections_answers_within_the_bound(self):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = start + timedelta(days=MONTH)
        built = time.perf_counter()
        self.fill_month(self.d1, start)
        built = time.perf_counter() - built
        with connect() as db:
            measured = {}
            for name, call in (("insights by hour", lambda: insights.counts(db, {self.d1: "Dock 1"}, start, end, "hour", now=end + timedelta(days=1))),
                               ("insights by day", lambda: insights.counts(db, {self.d1: "Dock 1"}, start, end, "day", now=end + timedelta(days=1))),
                               ("heatmap", lambda: insights.heatmap(db, self.d1, "Dock 1", start, end, now=end + timedelta(days=1))),
                               ("heatmap of people", lambda: insights.heatmap(db, self.d1, "Dock 1", start, end, label="person", now=end + timedelta(days=1))),
                               ("insights, ragged ends", lambda: insights.counts(db, {self.d1: "Dock 1"}, start + timedelta(minutes=7), end - timedelta(minutes=7), "hour",
                                                                                 now=end + timedelta(days=1)))):
                call()  # Warm the page cache, as a database in use is
                taken = min(self._timed(call) for _ in range(3))
                measured[name] = taken
            body = insights.counts(db, {self.d1: "Dock 1"}, start, end, "hour", now=end + timedelta(days=1))
            self.assertEqual(sum(sum(row) for row in body["counts"][f"camera:{self.d1}"]),
                             db.execute("SELECT COUNT(*) FROM insight_begins WHERE camera_id=? AND label IN ('person','vehicle')", (self.d1,)).fetchone()[0])
            rows = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("insight_begins", "insight_events", "insight_heat", "insight_heat_tracks")}
        print(f"\n#515 totals for 31 days of one camera ({DETECTIONS:,} detections, {TRACKS:,} tracks; rows {rows}; written in {built:.1f} s):")
        for name, taken in measured.items():
            print(f"  {name}: {taken * 1000:.0f} ms")
        for name, taken in measured.items():
            self.assertLess(taken, BOUND_SECONDS, name)

    def test_what_reading_the_rows_costs_without_totals(self):
        """The old reading, measured on a smaller generated camera and scaled to the ticket's: what the totals replace."""
        generate([self.d1], seed=3, tracks_per_camera=4000, days=2)
        start, end = BASE, BASE + timedelta(days=2)
        with connect() as db:
            detections = db.execute("SELECT COUNT(*) FROM ai_detections WHERE camera_id=?", (self.d1,)).fetchone()[0]
            counts_taken = min(self._timed(lambda: insights.counts(db, {self.d1: "Dock 1"}, start, end, "hour", now=LATER)) for _ in range(2))
            heat_taken = min(self._timed(lambda: insights.heatmap(db, self.d1, "Dock 1", start, end, now=LATER)) for _ in range(2))
        insight_totals.catch_up(LATER)
        with connect() as db:
            counts_after = min(self._timed(lambda: insights.counts(db, {self.d1: "Dock 1"}, start, end, "hour", now=LATER)) for _ in range(2))
            heat_after = min(self._timed(lambda: insights.heatmap(db, self.d1, "Dock 1", start, end, now=LATER)) for _ in range(2))
        scale = DETECTIONS / detections
        print(f"\n#515 reading the rows: {detections:,} detections; Insights {counts_taken * 1000:.0f} ms ({counts_taken / detections * 1e6:.2f} µs a detection, "
              f"about {counts_taken * scale:.0f} s at {DETECTIONS:,}), heatmap {heat_taken * 1000:.0f} ms (about {heat_taken * scale:.0f} s); "
              f"from the totals: Insights {counts_after * 1000:.0f} ms, heatmap {heat_after * 1000:.0f} ms")
        self.assertLess(counts_after, counts_taken)
        self.assertLess(heat_after, heat_taken)

    @staticmethod
    def _timed(call) -> float:
        started = time.perf_counter()
        call()
        return time.perf_counter() - started


class TriggerCostTests(TotalsCase):
    def test_storing_detections_costs_little_more(self):
        """What the triggers add to the AI worker's writes: a detection stored after the last of its track, the usual case."""
        def store(count: int) -> float:
            started = time.perf_counter()
            with connect() as db:
                db.execute("BEGIN")
                for n in range(count // 10):
                    track_id = str(uuid.uuid4())
                    at = BASE + timedelta(seconds=n)
                    db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES(?,?,'person',?,?,'m','1','CPU')",
                               (track_id, self.d1, _iso(at), _iso(at)))
                    for step in range(10):
                        db.execute("INSERT INTO ai_detections(camera_id,track_id,detected_at,label,confidence,box_x,box_y,box_width,box_height,model_id,model_version,provider) "
                                   "VALUES(?,?,?,'person',0.9,0.5,0.5,0.1,0.1,'m','1','CPU')", (self.d1, track_id, _iso(at + timedelta(milliseconds=100 * step))))
                db.execute("COMMIT")
            return time.perf_counter() - started

        store(5_000)  # Warm up
        with_triggers = min(store(20_000), store(20_000))
        with connect() as db:
            for name in ("insight_detection_added", "insight_track_added", "insight_track_changed"):
                db.execute(f"DROP TRIGGER {name}")
        without = min(store(20_000), store(20_000))
        print(f"\n#515 storing 20,000 detections: {with_triggers * 1000:.0f} ms with the totals' triggers, {without * 1000:.0f} ms without "
              f"({(with_triggers - without) / 20_000 * 1e6:.1f} µs a detection)")
        self.assertLess(with_triggers, without * 2 + 0.5)

