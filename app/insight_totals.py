"""Insights and heatmap totals, so a month of a busy camera answers in well under a second (#515).

Insights and the heatmap used to add up the stored tracks and detections on every request: a busy camera's month (10 million detections)
took minutes. The totals (migration 109) hold what those requests add up, a camera and an hour at a time. The readers in ``app.insights``
use an hour's totals when it is marked current and was worked out under the camera's present masks and rules, and read every other part
of the range (its ragged ends, hours not worked out yet, the last hour or so) from the tracks and detections exactly as they always did, so
the answer is the same either way.

**Upkeep.** The triggers of migration 109 take the "current" mark off every hour a write can change, in the writer's own transaction. Here,
in the background worker, each pass works out the hours of each camera that are not current, newest first, from the camera's first track to
the last hour that has settled (older than the thinning horizon, so thinning has finished with it). An hour is claimed (marked 0), worked out
from the stored rows without holding the write lock, then written with its mark set to 1 only if nothing removed the claim meanwhile. A
camera whose masks or rules have changed is started again. The work is resumable: what is done stays done, and an idle pass reads only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.clock import parse as _parse

from . import detection_thinning, insights, search, track_privacy
from .store import connect, has_row

logger = logging.getLogger(__name__)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
SLOT_SECONDS = 900  # Counts are kept per quarter hour: a site's hour or day starts on one in every time zone in use
HOUR_SECONDS = 3600
SETTLE_MARGIN_SECONDS = 300  # Beyond the thinning horizon before an hour is worked out
PASS_SECONDS = 2.0  # Work in one pass, at most; another follows at once while hours are waiting
POLL_SECONDS = 30.0
BUSY, DONE, ODD = 0, 1, 2  # insight_fresh.state: being worked out, current, current but read from the detections


def floor_units(moment: datetime, size: int) -> int:
    """Whole units of ``size`` seconds since 1970 before ``moment``."""
    return (moment - EPOCH) // timedelta(seconds=size)


def ceil_units(moment: datetime, size: int) -> int:
    units = floor_units(moment, size)
    return units if EPOCH + timedelta(seconds=units * size) == moment else units + 1


def unit_start(units: int, size: int) -> datetime:
    return EPOCH + timedelta(seconds=units * size)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def fingerprint(rules: Iterable[Any], masks: list[dict[str, float]]) -> str:
    """What a camera's totals depend on besides its rows: its privacy masks, and the zones and lines (and the classes they watch) of the
    enabled rules on it."""
    shapes = sorted([rule.id, rule.condition_type, rule.condition, sorted(rule.classes)] for rule in rules)
    return hashlib.sha256(json.dumps([shapes, masks], sort_keys=True, default=str).encode()).hexdigest()


def fingerprints(db: sqlite3.Connection, camera_ids: list[str]) -> tuple[dict[str, str], dict[str, list[Any]], track_privacy.Zones]:
    """Each camera's fingerprint now, with the rules (zone and line) on it and the masks."""
    defined: dict[str, list[Any]] = {camera_id: [] for camera_id in camera_ids}
    for entry in insights.definitions(db, camera_ids):
        defined[str(entry["camera_id"])].append(entry["rule"])
    zones = track_privacy.zones_by_camera(db, camera_ids)
    return {camera_id: fingerprint(defined[camera_id], zones.get(camera_id, [])) for camera_id in camera_ids}, defined, zones


def worked_out(db: sqlite3.Connection, prints: dict[str, str], first_hour: int, last_hour: int) -> dict[str, set[int]]:
    """The hours in [first_hour, last_hour] whose totals the readers may use, per camera: current, and worked out under the camera's
    present fingerprint."""
    if not prints:
        return {}
    ids = list(prints)
    stored = dict(db.execute(f"SELECT camera_id,fingerprint FROM insight_state WHERE camera_id IN ({','.join('?' * len(ids))})", ids).fetchall())
    found: dict[str, set[int]] = {camera_id: set() for camera_id in ids}
    for camera_id in ids:
        if stored.get(camera_id) != prints[camera_id]:
            continue
        found[camera_id] = {row[0] for row in db.execute("SELECT hour FROM insight_fresh WHERE camera_id=? AND hour BETWEEN ? AND ? AND state=?",
                                                         (camera_id, first_hour, last_hour, DONE))}
    return found


# --- reading -----------------------------------------------------------------------------------------------------------------------------


def runs(units: Iterable[int]) -> list[tuple[int, int]]:
    """Consecutive numbers as [first, last] pairs."""
    found: list[tuple[int, int]] = []
    for unit in sorted(units):
        if found and unit == found[-1][1] + 1:
            found[-1] = (found[-1][0], unit)
        else:
            found.append((unit, unit))
    return found


def gaps(start: datetime, end: datetime, units: Iterable[int], size: int) -> list[tuple[datetime, datetime]]:
    """The parts of [start, end) the given whole units (of ``size`` seconds, inside it) do not cover."""
    found: list[tuple[datetime, datetime]] = []
    cursor = start
    for first, last in runs(units):
        begin, finish = unit_start(first, size), unit_start(last + 1, size)
        if begin > cursor:
            found.append((cursor, begin))
        cursor = finish
    if cursor < end:
        found.append((cursor, end))
    return found


def add_counts(db: sqlite3.Connection, camera_id: str, bucket_of: dict[int, int], own: list[list[int]], defined: list[tuple[Any, list[list[int]]]],
               labels: list[str], end: datetime) -> None:
    """Add the totals of these quarter hours (each wholly inside one bucket) to the camera's and its zones' and lines' cells."""
    if not bucket_of:
        return
    first, last = min(bucket_of), max(bucket_of)
    # A track counts in the quarter hour it began if it was seen unmasked before the range ends (it began inside the range, so not before)
    for slot, label, n in db.execute("SELECT slot,label,COUNT(*) FROM insight_begins WHERE camera_id=? AND slot BETWEEN ? AND ? AND visible_at<? GROUP BY slot,label",
                                     (camera_id, first, last, _iso(end))):
        if slot in bucket_of and label in labels:
            own[bucket_of[slot]][insights.LABELS.index(label)] += n
    cells_of = {rule.id: (rule, cells) for rule, cells in defined}
    if not cells_of:
        return
    for slot, rule_id, label, n in db.execute("SELECT slot,rule_id,label,SUM(n) FROM insight_events WHERE camera_id=? AND slot BETWEEN ? AND ? GROUP BY slot,rule_id,label",
                                              (camera_id, first, last)):
        if slot in bucket_of and rule_id in cells_of and label in labels and label in cells_of[rule_id][0].classes:
            cells_of[rule_id][1][bucket_of[slot]][insights.LABELS.index(label)] += n


def add_heat(db: sqlite3.Connection, camera_id: str, hours: set[int], label: str | None, cells: list[list[int]]) -> list[tuple[str, list[Any]]]:
    """Add these hours' heatmap totals to the cells; return the queries (SQL, values) for the ids of the tracks seen in them."""
    track_queries = []
    labelled = " AND label=?" if label else ""
    for first, last in runs(hours):
        values = [camera_id, first, last, *([label] if label else [])]
        for cell, samples in db.execute(f"SELECT cell,SUM(samples) FROM insight_heat WHERE camera_id=? AND hour BETWEEN ? AND ?{labelled} GROUP BY cell", values):
            cells[cell // insights.HEAT_COLUMNS][cell % insights.HEAT_COLUMNS] += samples
        track_queries.append((f"SELECT track_id FROM insight_heat_tracks WHERE camera_id=? AND hour BETWEEN ? AND ?{labelled}", values))
    return track_queries


# --- working an hour out -----------------------------------------------------------------------------------------------------------------


def work_out(db: sqlite3.Connection, camera_id: str, hour: int, rules: list[Any], zones: track_privacy.Zones) -> dict[str, Any]:
    """The totals of one camera's hour, from its stored tracks and detections, by the same reading Insights and the heatmap do."""
    begin, finish = unit_start(hour, HOUR_SECONDS), unit_start(hour + 1, HOUR_SECONDS)
    since, until = _iso(begin), _iso(finish)
    masked = camera_id in zones
    permitted, permitted_values = track_privacy.detection_visible_sql(zones, "d")
    odd = False

    begins = []
    for row in db.execute("SELECT id,label,first_seen_at FROM ai_tracks WHERE camera_id=? AND first_seen_at>=? AND first_seen_at<?", (camera_id, since, until)).fetchall():
        began = _parse(row["first_seen_at"])
        slot = floor_units(began, SLOT_SECONDS)
        visible: str | None = ""
        if masked:
            first = db.execute(f"SELECT d.detected_at FROM ai_detections d WHERE d.track_id=? AND {permitted} ORDER BY d.detected_at LIMIT 1",
                               (row["id"], *permitted_values)).fetchone()
            visible = first[0] if first is not None else None
            if visible is not None and _parse(visible) < unit_start(slot, SLOT_SECONDS):
                odd = True  # Seen before it began: the range's start would matter, which these totals do not keep
        if floor_units(began, HOUR_SECONDS) != hour:
            odd = True  # A stored time whose order as text is not its order in time
        begins.append((camera_id, slot, row["id"], row["label"], visible))

    events: dict[tuple[int, str, str], int] = {}
    watched = set().union(*(rule.classes for rule in rules)) & set(insights.LABELS) if rules else set()
    if watched:
        first_slot = hour * 4
        tallies = {rule.id: [[0] * len(insights.LABELS) for _ in range(4)] for rule in rules}
        defined = [(rule, tallies[rule.id]) for rule in rules]

        def index(moment: datetime) -> int | None:
            return floor_units(moment, SLOT_SECONDS) - first_slot if begin <= moment < finish else None

        cursor = db.execute(f"SELECT t.id,t.label FROM ai_tracks t WHERE t.camera_id=? AND t.first_seen_at>=? AND t.first_seen_at<? AND t.last_seen_at>=? "
                            f"AND t.label IN ({','.join('?' * len(watched))})", (camera_id, search.began_since(db, [camera_id], begin), until, since, *sorted(watched)))
        while batch := cursor.fetchmany(insights.CHUNK):
            label_of = {row["id"]: row["label"] for row in batch}
            insights._count_moments(db, list(label_of), label_of, defined, index, permitted, permitted_values)
        for rule_id, cells in tallies.items():
            for offset, row in enumerate(cells):
                for position, n in enumerate(row):
                    if n:
                        events[(first_slot + offset, rule_id, insights.LABELS[position])] = n

    clauses = f"d.camera_id=? AND d.detected_at>=? AND d.detected_at<? AND {permitted}"
    values = [camera_id, since, until, *permitted_values]
    heat = db.execute(
        f"SELECT t.label,min(CAST((d.box_x+d.box_width/2)*{insights.HEAT_COLUMNS} AS INTEGER),{insights.HEAT_COLUMNS - 1}) AS column,"
        f"min(CAST((d.box_y+d.box_height/2)*{insights.HEAT_ROWS} AS INTEGER),{insights.HEAT_ROWS - 1}) AS row,SUM(d.stands_for) AS samples "
        f"FROM ai_detections d JOIN ai_tracks t ON t.id=d.track_id WHERE {clauses} GROUP BY t.label,column,row", values).fetchall()
    seen = db.execute(f"SELECT DISTINCT t.label,d.track_id FROM ai_detections d JOIN ai_tracks t ON t.id=d.track_id WHERE {clauses}", values).fetchall()
    return {"begins": begins,
            "events": [(camera_id, slot, rule_id, label, n) for (slot, rule_id, label), n in events.items()],
            "heat": [(camera_id, hour, row["label"], row["row"] * insights.HEAT_COLUMNS + row["column"], row["samples"]) for row in heat],
            "seen": [(camera_id, hour, row[0], row[1]) for row in seen],
            "odd": odd}


def _write(db: sqlite3.Connection, statements: Iterable[tuple[str, tuple[Any, ...]]]) -> bool:
    """Run these in one write transaction; a statement that answers False (a check) rolls it back."""
    db.execute("BEGIN IMMEDIATE")
    try:
        for sql, values in statements:
            if sql == "CHECK":
                if not values[0](db):
                    db.execute("ROLLBACK")
                    return False
                continue
            db.execute(sql, values)
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
    return True


def settled_hour(db: sqlite3.Connection, now: datetime) -> int:
    """The last hour every writer has finished with: it ended longer ago than the thinning horizon and a margin."""
    return floor_units(now - timedelta(seconds=detection_thinning.horizon_seconds(db) + SETTLE_MARGIN_SECONDS), HOUR_SECONDS) - 1


def _first_hour(db: sqlite3.Connection, camera_id: str) -> int | None:
    first = db.execute("SELECT MIN(first_seen_at) FROM ai_tracks WHERE camera_id=?", (camera_id,)).fetchone()[0]
    if first is None:
        return None
    try:
        return floor_units(_parse(first), HOUR_SECONDS)
    except ValueError:
        return None


def _due_hours(db: sqlite3.Connection, camera_id: str, first: int, last: int) -> list[int]:
    """The hours in [first, last] that are not current, newest first. Counting first keeps the usual answer (none) to one index read."""
    if last < first:
        return []
    have = db.execute("SELECT COUNT(*) FROM insight_fresh WHERE camera_id=? AND hour BETWEEN ? AND ? AND state>?", (camera_id, first, last, BUSY)).fetchone()[0]
    if have >= last - first + 1:
        return []
    done = {row[0] for row in db.execute("SELECT hour FROM insight_fresh WHERE camera_id=? AND hour BETWEEN ? AND ? AND state>?", (camera_id, first, last, BUSY))}
    return [hour for hour in range(last, first - 1, -1) if hour not in done]


def _restart(db: sqlite3.Connection, camera_id: str, print_: str, masks: list[dict[str, float]]) -> None:
    """A camera's masks or rules changed (or it has never been worked out): nothing of it is current any more."""
    _write(db, [("DELETE FROM insight_fresh WHERE camera_id=?", (camera_id,)),
                ("DELETE FROM insight_masks WHERE camera_id=?", (camera_id,)),
                *(("INSERT INTO insight_masks(camera_id,x1,y1,x2,y2) VALUES(?,?,?,?,?)", (camera_id, z["x"], z["y"], z["x"] + z["width"], z["y"] + z["height"]))
                  for z in masks),
                ("INSERT INTO insight_state(camera_id,fingerprint) VALUES(?,?) ON CONFLICT(camera_id) DO UPDATE SET fingerprint=excluded.fingerprint",
                 (camera_id, print_))])


def work_out_hour(db: sqlite3.Connection, camera_id: str, hour: int, print_: str, rules: list[Any], zones: track_privacy.Zones) -> bool:
    """Claim, work out and write one hour; False when a write to it (or a change of masks or rules) came in meanwhile, so it waits."""
    _write(db, [("INSERT INTO insight_fresh(camera_id,hour,state) VALUES(?,?,?) ON CONFLICT(camera_id,hour) DO UPDATE SET state=excluded.state",
                 (camera_id, hour, BUSY))])
    totals = work_out(db, camera_id, hour, rules, zones)

    def unchanged(db: sqlite3.Connection) -> bool:
        claim = db.execute("SELECT state FROM insight_fresh WHERE camera_id=? AND hour=?", (camera_id, hour)).fetchone()
        state = db.execute("SELECT fingerprint FROM insight_state WHERE camera_id=?", (camera_id,)).fetchone()
        return claim is not None and claim[0] == BUSY and state is not None and state[0] == print_

    statements: list[tuple[str, Any]] = [("CHECK", (unchanged,))]
    statements.append(("DELETE FROM insight_begins WHERE camera_id=? AND slot BETWEEN ? AND ?", (camera_id, hour * 4, hour * 4 + 3)))
    statements.append(("DELETE FROM insight_events WHERE camera_id=? AND slot BETWEEN ? AND ?", (camera_id, hour * 4, hour * 4 + 3)))
    statements.append(("DELETE FROM insight_heat WHERE camera_id=? AND hour=?", (camera_id, hour)))
    statements.append(("DELETE FROM insight_heat_tracks WHERE camera_id=? AND hour=?", (camera_id, hour)))
    statements += [("INSERT INTO insight_begins(camera_id,slot,track_id,label,visible_at) VALUES(?,?,?,?,?)", row) for row in totals["begins"]]
    statements += [("INSERT INTO insight_events(camera_id,slot,rule_id,label,n) VALUES(?,?,?,?,?)", row) for row in totals["events"]]
    statements += [("INSERT INTO insight_heat(camera_id,hour,label,cell,samples) VALUES(?,?,?,?,?)", row) for row in totals["heat"]]
    statements += [("INSERT INTO insight_heat_tracks(camera_id,hour,label,track_id) VALUES(?,?,?,?)", row) for row in totals["seen"]]
    statements.append(("UPDATE insight_fresh SET state=? WHERE camera_id=? AND hour=?", (ODD if totals["odd"] else DONE, camera_id, hour)))
    return _write(db, statements)


def _prune(db: sqlite3.Connection, camera_id: str, first: int | None) -> None:
    """Totals of hours before the camera's first track (its older footage has expired), or of a camera that is gone."""
    below = (first if first is not None else 1 << 40)
    _write(db, [("DELETE FROM insight_fresh WHERE camera_id=? AND hour<?", (camera_id, below)),
                ("DELETE FROM insight_begins WHERE camera_id=? AND slot<?", (camera_id, below * 4)),
                ("DELETE FROM insight_events WHERE camera_id=? AND slot<?", (camera_id, below * 4)),
                ("DELETE FROM insight_heat WHERE camera_id=? AND hour<?", (camera_id, below)),
                ("DELETE FROM insight_heat_tracks WHERE camera_id=? AND hour<?", (camera_id, below))])


def _has_old(db: sqlite3.Connection, camera_id: str, first: int | None) -> bool:
    below = (first if first is not None else 1 << 40)
    return (has_row(db, "SELECT 1 FROM insight_fresh WHERE camera_id=? AND hour<? LIMIT 1", (camera_id, below))
            or has_row(db, "SELECT 1 FROM insight_begins WHERE camera_id=? AND slot<? LIMIT 1", (camera_id, below * 4))
            or has_row(db, "SELECT 1 FROM insight_events WHERE camera_id=? AND slot<? LIMIT 1", (camera_id, below * 4))
            or has_row(db, "SELECT 1 FROM insight_heat WHERE camera_id=? AND hour<? LIMIT 1", (camera_id, below))
            or has_row(db, "SELECT 1 FROM insight_heat_tracks WHERE camera_id=? AND hour<? LIMIT 1", (camera_id, below)))


def step(now: datetime | None = None, *, until: float | None = None, stop: threading.Event | None = None) -> dict[str, int]:
    """One pass over every camera: start again those whose masks or rules changed, drop totals older than a camera's first track, and work
    out hours that are not current, newest first, until ``until`` (a ``time.monotonic()`` deadline). Reads first: a pass with nothing to do
    takes no write lock (#280). Says how many hours it worked out and whether more are waiting."""
    now = now or datetime.now(timezone.utc)
    done = {"hours": 0, "waiting": 0, "restarted": 0}
    with connect() as db:
        cameras = [row[0] for row in db.execute("SELECT id FROM cameras WHERE archived_at IS NULL ORDER BY id")]
        for gone in [row[0] for row in db.execute("SELECT camera_id FROM insight_state WHERE camera_id NOT IN (SELECT id FROM cameras)")]:
            _prune(db, gone, None)
            _write(db, [("DELETE FROM insight_state WHERE camera_id=?", (gone,)), ("DELETE FROM insight_masks WHERE camera_id=?", (gone,))])
        if not cameras:
            return done
        prints, defined, zones = fingerprints(db, cameras)
        stored = dict(db.execute("SELECT camera_id,fingerprint FROM insight_state").fetchall())
        last = settled_hour(db, now)
        for camera_id in cameras:
            first = _first_hour(db, camera_id)
            if first is None and camera_id not in stored:
                continue  # Nothing to count yet
            if stored.get(camera_id) != prints[camera_id]:
                _restart(db, camera_id, prints[camera_id], zones.get(camera_id, []))
                done["restarted"] += 1
            if _has_old(db, camera_id, first):
                _prune(db, camera_id, first)
            if first is None:
                continue
            for hour in _due_hours(db, camera_id, first, last):
                if (until is not None and time.monotonic() >= until) or (stop is not None and stop.is_set()):
                    done["waiting"] += 1
                    break
                if work_out_hour(db, camera_id, hour, prints[camera_id], defined[camera_id], zones):
                    done["hours"] += 1
    return done


def catch_up(now: datetime | None = None) -> dict[str, int]:
    """Every pass needed until nothing is waiting (tests, and a salvage's aftermath)."""
    total = {"hours": 0, "waiting": 0, "restarted": 0}
    while True:
        done = step(now)
        total = {key: total[key] + done[key] for key in total}
        if not done["hours"]:
            return total


class TotalsManager:
    """Keeps the totals current in the background worker: a pass of at most ``PASS_SECONDS`` of work, again at once while hours are
    waiting, otherwise every ``poll_seconds``."""

    def __init__(self, poll_seconds: float = POLL_SECONDS, pass_seconds: float = PASS_SECONDS) -> None:
        self.poll_seconds, self.pass_seconds = poll_seconds, pass_seconds
        self.task: asyncio.Task[None] | None = None
        self.stopping = threading.Event()

    async def start(self) -> None:
        self.stopping.clear()
        self.task = asyncio.create_task(self._run(), name="insight-totals")

    async def stop(self) -> None:
        self.stopping.set()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    def pass_once(self) -> bool:
        return bool(step(until=time.monotonic() + self.pass_seconds, stop=self.stopping)["waiting"])

    async def _run(self) -> None:
        while True:
            waiting = False
            try:
                waiting = await asyncio.to_thread(self.pass_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the totals are a cache: a failed pass is tried again, and the readers read what is not current
                logger.exception("Working out the Insights totals failed")
            await asyncio.sleep(0 if waiting else self.poll_seconds)
