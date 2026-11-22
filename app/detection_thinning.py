"""An object that is not moving does not keep a row for every analysed frame (#512).

Every detection of every analysed frame was kept until its footage expired: a car park camera with twenty parked cars stored about
265 MB a day. What is kept now, by the owner's decision on the ticket:

* an object's box when it has **moved by more than a fiftieth of the picture** (``MOVED``) since the last box kept, or **every
  30 seconds** (``HOLD_SECONDS``), whichever comes first;
* its **first and last** sighting, always;
* any box whose answer to "is it inside this rule's zone, on which side of this rule's line, does it touch a privacy mask" differs from
  the last box kept, so a zone, a line and a mask see the same crossings as before.

**Rows are thinned after the fact, never left out as they are stored.** The AI worker goes on storing every frame, so everything that
works on what is happening now (rules, alarms, Live's object picker, PTZ following) sees exactly what it saw before. Once a frame is
older than ``horizon_seconds`` (ten minutes, or the longest time any enabled rule looks back, if that is more) *and* the rules worker has
evaluated it, the rows of a still object between two kept ones are deleted. The kept row before them counts them in ``stands_for``.

**What reads thinned rows.** A kept row with ``stands_for`` = n stands for n analysed frames: its own and the n-1 after it, up to the
object's next row. ``expand`` gives those frames back (the same box, at evenly spaced times), so a reader that wants a point for each
analysed frame (rules in test mode and after a restart, occupancy, Playback's overlay, Insights) gets the same number of points at the
same times, each within ``MOVED`` of where the object was. Counts use ``SUM(stands_for)``; a track's ``detection_count`` is unchanged.

Rows stored before this version are never touched (the cursor starts at the newest row there was): they age out with retention.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .store import connect

logger = logging.getLogger(__name__)
MOVED = 0.02  # Of the picture's width or height: a box's centre or size changing by more than this is movement
HOLD_SECONDS = 30  # A still object's box is kept at least this often
MIN_AGE_SECONDS = 600  # Nothing younger than this is thinned
RULE_MARGIN_SECONDS = 120  # Beyond the longest time an enabled rule looks back
BATCH = 1000  # Rows looked at in one step: the write lock is held for the deletes of one step (about 50 ms)
STEPS_PER_PASS = 20
PASS_SECONDS = 30.0
ROW_COLUMNS = "id,camera_id,track_id,detected_at,box_x,box_y,box_width,box_height,stands_for"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def horizon_seconds(db: sqlite3.Connection) -> float:
    """How old a frame must be before it may be thinned: older than anything an enabled rule still looks back over."""
    import json

    longest = 0.0
    for row in db.execute("SELECT condition_json FROM rules WHERE enabled=1"):
        try:
            condition = json.loads(row["condition_json"])
            longest = max(longest, *(float(condition.get(key) or 0) for key in ("dwell_seconds", "still_seconds", "for_seconds")))
        except (ValueError, TypeError, AttributeError):
            continue
    return max(MIN_AGE_SECONDS, longest + RULE_MARGIN_SECONDS)


class _Shapes:
    """What decides, besides distance, whether a box of one kind of object must be kept on one camera: the camera's privacy masks and the
    zones and lines of the rules that watch it for that kind. A box whose answers differ from the last kept box's is kept."""

    def __init__(self, db: sqlite3.Connection, camera_id: str, label: str) -> None:
        from . import rules, track_privacy

        self.masks = track_privacy.zones_by_camera(db, [camera_id]).get(camera_id, [])
        self.zones: list[dict[str, float]] = []
        self.lines: list[tuple[float, float, float, float]] = []
        try:
            groups = rules._camera_groups(db, camera_id)
            for rule in rules.load_rules(db):
                if not rule.watches_camera(camera_id, groups) or label not in rule.classes:
                    continue
                zone, line = rule.condition.get("zone"), rule.condition.get("line")
                if isinstance(zone, dict):
                    self.zones.append(zone)
                if isinstance(line, dict):
                    self.lines.append((line["x1"], line["y1"], line["x2"], line["y2"]))
        except Exception:  # noqa: BLE001 - a rule that cannot be read does not stop thinning; distance and time still decide
            logger.exception("The rules of camera %s could not be read for thinning", camera_id)

    def answers(self, row: Any) -> tuple[Any, ...]:
        from . import track_privacy

        box = {"x": row["box_x"], "y": row["box_y"], "width": row["box_width"], "height": row["box_height"]}
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        inside = tuple(zone["x"] <= x <= zone["x"] + zone["width"] and zone["y"] <= y <= zone["y"] + zone["height"] for zone in self.zones)
        side = tuple((x - ax) * (by - ay) - (y - ay) * (bx - ax) >= 0 for ax, ay, bx, by in self.lines)
        return (bool(self.masks) and track_privacy.box_touches(box, self.masks), inside, side)


def _moved(row: Any, kept: Any) -> bool:
    return (abs((row["box_x"] + row["box_width"] / 2) - (kept["box_x"] + kept["box_width"] / 2)) > MOVED
            or abs((row["box_y"] + row["box_height"] / 2) - (kept["box_y"] + kept["box_height"] / 2)) > MOVED
            or abs(row["box_width"] - kept["box_width"]) > MOVED or abs(row["box_height"] - kept["box_height"]) > MOVED)


def thin_step(now: datetime | None = None) -> dict[str, int]:
    """Look at the next rows old enough to thin, and delete those a kept row can stand for. Bounded; reads first, and takes the write lock
    only when there are rows to move the cursor past (#280)."""
    now = now or datetime.now(timezone.utc)
    done = {"looked": 0, "removed": 0}
    with connect() as db:
        cursor = db.execute("SELECT last_id FROM detection_thinning WHERE singleton_id=1").fetchone()
        evaluated = db.execute("SELECT last_detection_id FROM rules_cursor WHERE singleton_id=1").fetchone()
        if cursor is None or evaluated is None:
            return done
        cursor = int(cursor[0])
        fresh = db.execute(f"SELECT {ROW_COLUMNS} FROM ai_detections WHERE id>? AND id<=? ORDER BY id LIMIT ?", (cursor, int(evaluated[0]), BATCH)).fetchall()
        if not fresh:
            return done
        limit = now - timedelta(seconds=horizon_seconds(db))
        rows = []
        for row in fresh:
            if _parse(row["detected_at"]) >= limit:
                break  # Too young, and so is what follows: rows are stored in the order they happen
            rows.append(row)
        if not rows:
            return done
        by_track: dict[str, list[Any]] = {}
        for row in rows:
            by_track.setdefault(row["track_id"], []).append(row)
        shapes: dict[tuple[str, str], _Shapes] = {}
        doomed: list[int] = []
        stands: dict[int, int] = {}  # Kept row -> how many more frames it now stands for
        restored: dict[str, int] = {}  # Track -> frames its deleted rows stood for
        for track_id, track_rows in by_track.items():
            track_rows.sort(key=lambda row: (row["detected_at"], row["id"]))
            track = db.execute("SELECT last_seen_at,label FROM ai_tracks WHERE id=?", (track_id,)).fetchone()
            if track is None:
                continue
            key = (track_rows[0]["camera_id"], track["label"])
            if key not in shapes:
                shapes[key] = _Shapes(db, *key)
            camera = shapes[key]
            # The last row kept before these: everything of this track before the cursor has already been thinned.
            kept = db.execute(f"SELECT {ROW_COLUMNS} FROM ai_detections WHERE track_id=? AND detected_at<? ORDER BY detected_at DESC,id DESC LIMIT 1",
                              (track_id, track_rows[0]["detected_at"])).fetchone()
            kept_answers = camera.answers(kept) if kept is not None else None
            for row in track_rows:
                answers = camera.answers(row)
                if (kept is None or row["detected_at"] == track["last_seen_at"] or answers != kept_answers or _moved(row, kept)
                        or (_parse(row["detected_at"]) - _parse(kept["detected_at"])).total_seconds() >= HOLD_SECONDS):
                    kept, kept_answers = row, answers
                    continue
                doomed.append(row["id"])
                stands[kept["id"]] = stands.get(kept["id"], 0) + int(row["stands_for"])
                restored[track_id] = restored.get(track_id, 0) + int(row["stands_for"])
        db.execute("BEGIN IMMEDIATE")
        try:
            if int(db.execute("SELECT last_id FROM detection_thinning WHERE singleton_id=1").fetchone()[0]) != cursor:
                db.execute("ROLLBACK")
                return done  # Another pass took these rows
            for start in range(0, len(doomed), 500):
                part = doomed[start:start + 500]
                db.execute(f"DELETE FROM ai_detections WHERE id IN ({','.join('?' * len(part))})", part)
            db.executemany("UPDATE ai_detections SET stands_for=stands_for+? WHERE id=?", [(more, kept_id) for kept_id, more in stands.items()])
            # The trigger took each deleted row's frames off its track's count; the kept rows stand for them, so they are put back.
            db.executemany("UPDATE ai_tracks SET detection_count=detection_count+? WHERE id=?", [(frames, track_id) for track_id, frames in restored.items()])
            db.execute("UPDATE detection_thinning SET last_id=? WHERE singleton_id=1", (rows[-1]["id"],))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    done.update(looked=len(rows), removed=len(doomed))
    return done


def thin_pending(now: datetime | None = None) -> dict[str, int]:
    """A pass: a few steps, stopping when there is nothing old enough left."""
    total = {"looked": 0, "removed": 0}
    for _ in range(STEPS_PER_PASS):
        done = thin_step(now)
        total = {key: total[key] + done[key] for key in total}
        if done["looked"] < BATCH:
            break
    return total


def _copies(item: Any, until: str) -> list[dict[str, Any]]:
    """The frames a kept row stands for besides its own: the same box at evenly spaced times up to the object's next row at ``until``."""
    frames = int(item["stands_for"])
    start, end = _parse(item["detected_at"]), _parse(until)
    zulu = item["detected_at"].endswith("Z")
    held = []
    for frame in range(1, frames):
        at = (start + (end - start) * frame / frames).isoformat()
        held.append({**dict(item), "detected_at": at.replace("+00:00", "Z") if zulu else at, "stands_for": 1, "held": True})
    return held


def _next_at(db: sqlite3.Connection, item: Any) -> str | None:
    following = db.execute("SELECT detected_at FROM ai_detections WHERE track_id=? AND detected_at>? ORDER BY detected_at,id LIMIT 1",
                           (item["track_id"], item["detected_at"])).fetchone()
    return following["detected_at"] if following is not None else None


def expand_by_track(db: sqlite3.Connection, rows: Iterable[Any]) -> Iterable[Any]:
    """``expand`` for rows that arrive a track at a time (ordered by track, then time), as a stream: nothing is held but a thinned row
    waiting for the row after it."""
    waiting: Any = None
    for row in rows:
        if waiting is not None:
            until = row["detected_at"] if row["track_id"] == waiting["track_id"] else _next_at(db, waiting)
            if until is not None:
                yield from _copies(waiting, until)
            waiting = None
        yield row
        if row["stands_for"] > 1:
            waiting = row
    if waiting is not None and (until := _next_at(db, waiting)) is not None:
        yield from _copies(waiting, until)


def expand(db: sqlite3.Connection, rows: Iterable[Any]) -> list[Any]:
    """The rows with a point for each analysed frame they stand for, in (time, id) order.

    A row with ``stands_for`` = n is followed by n-1 copies of itself at evenly spaced times up to the object's next row (looked up when
    it is not among ``rows``). The copies carry ``held: True`` and the kept row's own id. Rows that stand only for themselves, which is
    every row younger than the thinning horizon and every row stored before thinning, come back unchanged."""
    stored = list(rows)
    if not any(row["stands_for"] > 1 for row in stored):
        return stored  # The usual case, and the whole of the live path: nothing is copied
    by_track: dict[str, list[Any]] = {}
    for row in stored:
        by_track.setdefault(row["track_id"], []).append(row)
    held: list[dict[str, Any]] = []
    for track_rows in by_track.values():
        track_rows.sort(key=lambda item: (item["detected_at"], item["id"]))
        for index, item in enumerate(track_rows):
            if item["stands_for"] <= 1:
                continue
            until = track_rows[index + 1]["detected_at"] if index + 1 < len(track_rows) else _next_at(db, item)
            if until is not None:
                held.extend(_copies(item, until))
    return sorted([*stored, *held], key=lambda item: (item["detected_at"], item["id"]))
