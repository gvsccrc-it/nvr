"""Occupancy rules (#254): more than a set number of people (or vehicles) in the frame, or in a zone, for a set time.

Unlike the other rules this one looks at several tracks at once, so it is worked out from the camera's stored detections rather than
from one track. An object counts at a moment if it was detected within ``PRESENT_WITHIN`` of it (the AI does not look at every frame), and
inside the zone when the rule has one. The rule fires once for each stretch of time the count stays above the limit for the whole set time:
``exceedances`` over a stored range (test mode), ``Stretch`` as detections arrive (the rules worker). A rule may ask to be reminded while a
stretch goes on (``remind_minutes``, at least 15); a reminder is a note and a notification on the stretch's one alarm, never a new alarm.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

PRESENT_WITHIN = 2  # seconds either side of a moment
MIN_COOLDOWN = timedelta(seconds=60)
WINDOW_MARGIN = 10  # seconds of history beyond the set time, so a stretch already under way is seen whole
FULL_FRAME = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}


def exceedances(detections: Iterable[tuple[str, datetime, float, float]], condition: dict[str, Any]) -> list[datetime]:
    """When each stretch above the limit has lasted the set time. ``detections`` are (track id, time, centre x, centre y)."""
    zone, limit, hold = condition.get("zone"), condition["limit"], condition["for_seconds"]
    by_second: dict[int, set[str]] = defaultdict(set)
    first_at: dict[int, datetime] = {}
    for track_id, at, x, y in detections:
        if zone is not None and not (zone["x"] <= x <= zone["x"] + zone["width"] and zone["y"] <= y <= zone["y"] + zone["height"]):
            continue
        second = int(at.timestamp())
        by_second[second].add(track_id)
        first_at.setdefault(second, at)
    moments: list[datetime] = []
    streak_start: int | None = None
    last_over: int | None = None
    reported = False
    for second in sorted(by_second):
        present: set[str] = set()
        for near in range(second - PRESENT_WITHIN, second + PRESENT_WITHIN + 1):
            present |= by_second.get(near, set())
        if len(present) <= limit:
            streak_start, last_over, reported = None, None, False
            continue
        if streak_start is None or last_over is None or second - last_over > PRESENT_WITHIN:
            streak_start, reported = second, False
        last_over = second
        if not reported and second - streak_start >= hold:
            moments.append(first_at[second])
            reported = True
    return moments


END_QUIET = 10  # Seconds with no detection at all after which an over-limit stretch is taken to have ended (the tracker's own patience)
MIN_REMIND_MINUTES = 15


class Stretch:
    """One occupancy rule on one camera, followed as detections arrive (#513).

    The live pass used to re-read the last ``for_seconds`` of detections every second and run ``exceedances`` over them. Inside that
    window a stretch always seemed to begin at the window's first second, so "has lasted the set time" came true again on every pass and
    a room over its limit for two hours raised about 120 alarms. This keeps the same counting going from pass to pass instead: it is fed
    only the detections stored since the last pass, and remembers that the stretch it is in has been reported.

    A second's count includes whoever was detected up to ``PRESENT_WITHIN`` seconds after it, so a second is only settled once those have
    arrived. Settled seconds are counted exactly as ``exceedances`` counts them. The seconds not yet settled are counted with what is
    known so far, which can only be fewer people: when that is already over the limit for the set time, the stretch is reported at once,
    not ``PRESENT_WITHIN`` seconds later, and the settled count does not report it a second time.
    """

    def __init__(self, condition: dict[str, Any]) -> None:
        self.zone, self.limit, self.hold = condition.get("zone"), int(condition["limit"]), int(condition["for_seconds"])
        remind = condition.get("remind_minutes")
        self.remind = max(MIN_REMIND_MINUTES, int(remind)) * 60 if remind else None
        self.by_second: dict[int, set[str]] = {}
        self.first_at: dict[int, datetime] = {}
        self.settled: int | None = None  # The last second counted for good
        self.streak_start: int | None = None
        self.last_over: int | None = None
        self.reported = False
        self.issued: int | None = None  # The second the current stretch was reported at, by either count
        self.reminded: int | None = None  # The second of its report or its last reminder
        self.newest: tuple[str, int] | None = None  # (time as stored, id) of the last detection fed: where the next read starts

    def add(self, detections: Iterable[tuple[str, datetime, float, float]]) -> None:
        zone = self.zone
        for track_id, at, x, y in detections:
            if zone is not None and not (zone["x"] <= x <= zone["x"] + zone["width"] and zone["y"] <= y <= zone["y"] + zone["height"]):
                continue
            second = int(at.timestamp())
            self.by_second.setdefault(second, set()).add(track_id)
            self.first_at.setdefault(second, at)

    def _present(self, second: int) -> int:
        present: set[str] = set()
        for near in range(second - PRESENT_WITHIN, second + PRESENT_WITHIN + 1):
            present |= self.by_second.get(near, set())
        return len(present)

    def _count(self, second: int, state: list[Any]) -> bool:
        """``exceedances``'s step for one second, on ``state`` = [streak_start, last_over, reported]. True when the stretch reaches the set time."""
        if self._present(second) <= self.limit:
            state[:] = [None, None, False]
            return False
        if state[0] is None or state[1] is None or second - state[1] > PRESENT_WITHIN:
            state[0], state[2] = second, False
        state[1] = second
        if not state[2] and second - state[0] >= self.hold:
            state[2] = True
            return True
        return False

    def advance(self, newest_second: int) -> list[tuple[str, datetime]]:
        """Count what has arrived up to ``newest_second``. Returns what happened, in order: ("over", when) for a stretch that has lasted
        the set time, ("reminder", when) for one still going after the rule's reminder interval, ("ended", when) for a reported stretch
        whose count has dropped back."""
        events: list[tuple[str, datetime]] = []
        state = [self.streak_start, self.last_over, self.reported]
        # Settled: nothing more can arrive within PRESENT_WITHIN of these seconds (detections arrive in the order they happen).
        for second in sorted(key for key in self.by_second if (self.settled is None or key > self.settled) and key <= newest_second - PRESENT_WITHIN - 1):
            was_reported, was_over = state[2], state[1]
            reached = self._count(second, state)
            if was_reported and not state[2] and was_over is not None and not (reached and state[0] is not None and state[0] <= was_over):
                events.append(("ended", self._at(was_over)))  # The count dropped back, or there was a gap, after a stretch that had been reported
                self.issued = self.reminded = None
            if reached:
                if self.issued is not None and self.issued >= state[0]:
                    pass  # Reported already, on the count of the seconds not yet settled
                else:
                    self.issued = self.reminded = second
                    events.append(("over", self.first_at[second]))
            elif state[2] and self.remind and self.reminded is not None and second - self.reminded >= self.remind:
                self.reminded = second
                events.append(("reminder", self.first_at[second]))
            self.settled = second
        self.streak_start, self.last_over, self.reported = state
        # Not settled yet: counted with what is known, on a copy. Only ever fewer people than the settled count will find.
        ahead = list(state)
        for second in sorted(key for key in self.by_second if self.settled is None or key > self.settled):
            if self._count(second, ahead) and not (self.issued is not None and self.issued >= ahead[0]):
                self.issued = self.reminded = second
                events.append(("over", self.first_at[second]))
        keep_from = (self.settled if self.settled is not None else newest_second) - PRESENT_WITHIN
        for second in [key for key in self.by_second if key < keep_from]:
            del self.by_second[second]
            self.first_at.pop(second, None)
        return events

    def quiet(self) -> list[tuple[str, datetime]]:
        """No detection at all has arrived for the camera for ``END_QUIET`` seconds (the caller's clock): a reported stretch has ended,
        everyone having left at once or the camera no longer being analysed. The count starts again from nothing."""
        events = self.advance(max(self.by_second) + PRESENT_WITHIN + 1) if self.by_second else []  # Nothing more is coming: the last seconds are settled as they are
        ended = [*events, ("ended", self._at(max(value for value in (self.last_over, self.issued) if value is not None)))] if self.issued is not None else events
        self.by_second.clear()
        self.first_at.clear()
        self.streak_start = self.last_over = self.issued = self.reminded = None
        self.reported = False
        return ended

    def _at(self, second: int) -> datetime:
        return self.first_at.get(second) or datetime.fromtimestamp(second, tz=timezone.utc)


def window_rows(db: sqlite3.Connection, camera_id: str, classes: Iterable[str], start: datetime, end: datetime) -> list[tuple[str, datetime, float, float]]:
    """Detections of the classes on a camera in [start, end), as ``exceedances`` takes them."""
    labels = list(classes)
    marks = ",".join("?" for _ in labels)
    from . import detection_thinning

    # A still object's thinned rows stand for the frames between them (#512): read from a hold before the window, so an object standing
    # there since before it is counted, and given back as a point for each analysed frame.
    reach = start - timedelta(seconds=detection_thinning.HOLD_SECONDS)
    rows = detection_thinning.expand(db, db.execute(
        f"SELECT id,track_id,detected_at,box_x,box_y,box_width,box_height,stands_for FROM ai_detections WHERE camera_id=? AND label IN ({marks}) AND detected_at>=? AND detected_at<? ORDER BY detected_at",
        (camera_id, *labels, reach.isoformat(), end.isoformat())))
    result = []
    for row in rows:
        at = datetime.fromisoformat(row["detected_at"].replace("Z", "+00:00"))
        if not start <= at < end:
            continue
        result.append((row["track_id"], at, row["box_x"] + row["box_width"] / 2, row["box_y"] + row["box_height"] / 2))
    return result


def rows_since(db: sqlite3.Connection, camera_id: str, classes: Iterable[str], since: str, after: tuple[str, int] | None,
               up_to_id: int) -> tuple[list[tuple[str, datetime, float, float]], tuple[str, int] | None]:
    """The camera's detections of the classes from ``since`` (a time as stored) that come after ``after`` and were stored up to
    ``up_to_id``, as ``Stretch.add`` takes them, and the (time, id) of the last one. Read through the camera's time index: a pass costs
    what arrived since the last one, however long the rule's set time is."""
    labels = list(classes)
    marks = ",".join("?" for _ in labels)
    found, last = [], after
    for row in db.execute(f"SELECT id,track_id,detected_at,box_x,box_y,box_width,box_height FROM ai_detections WHERE camera_id=? AND label IN ({marks}) AND detected_at>=? "
                          "ORDER BY detected_at,id", (camera_id, *labels, since)):
        key = (row["detected_at"], int(row["id"]))
        if row["id"] > up_to_id or (after is not None and key <= after):
            continue
        found.append((row["track_id"], datetime.fromisoformat(row["detected_at"].replace("Z", "+00:00")), row["box_x"] + row["box_width"] / 2, row["box_y"] + row["box_height"] / 2))
        last = key if last is None or key > last else last
    return found, last


def window_for(condition: dict[str, Any], newest: datetime) -> tuple[datetime, datetime]:
    return newest - timedelta(seconds=condition["for_seconds"] + WINDOW_MARGIN + PRESENT_WITHIN), newest + timedelta(seconds=PRESENT_WITHIN + 1)
