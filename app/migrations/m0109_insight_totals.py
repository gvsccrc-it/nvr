"""Insights and heatmap totals, worked out an hour at a time and kept current by triggers (#515).

Insights counted every track in range in Python and, on a camera with a zone or line rule, read every detection of those tracks; the heatmap
added up every detection in range, twice. A month of a busy camera (10 million detections) took minutes. The totals here are what those
readers add up, an hour (a quarter of an hour for counts) at a time:

- ``insight_begins``: each track, the quarter hour it began in, its label, and when its first detection a privacy mask leaves visible was
  ('' on a camera with no mask: every track there is visible; NULL when none is). Insights counts a track in the quarter hour it began,
  if it was seen unmasked before the end of the range.
- ``insight_events``: each quarter hour's zone entries and line crossings, per rule and label.
- ``insight_heat`` and ``insight_heat_tracks``: each hour's heatmap cells per label (frames, as ``SUM(stands_for)``) and the tracks seen.
- ``insight_fresh``: the hours of a camera whose totals are current (``state`` 1), or current but not to be used (2: a stored time that
  cannot be compared safely, so the readers read that hour from the detections as before); 0 while an hour is being worked out.
- ``insight_state``: what a camera's totals were worked out under (its masks and the zones and lines of the rules on it). The readers use
  the totals only while it matches; when it does not, the background works the camera out again.
- ``insight_masks``: the camera's masks as rectangles, for the trigger below.

Nothing is worked out here, so the upgrade is instant on any database: the background (``app.insight_totals``) works out each hour once it
has settled, newest first, and the readers read hours not yet worked out from the detections exactly as before.

The triggers remove the "current" mark from every hour a change can affect, in the writer's own transaction:

- a detection added, removed or moved: its hour, and every later hour of its track (a zone is counted once per track and a line crossing
  needs the point before, so taking out a point can move a later count); and the hour the track began in when the detection is, or may now
  be, the track's first unmasked one;
- a detection's ``stands_for`` changed (thinning): its hour only (the heatmap counts frames; thinning keeps every point where a zone, a line
  or a mask answers differently, so no zone or line count changes);
- a track added, removed, relabelled, or its first time changed: every hour it spans.

A detection added after the last one of its track (what the AI worker does) touches only its own hour, which is not worked out yet.
"""
from __future__ import annotations

import sqlite3

NAME = "insight_totals"


def hour_of(value: str) -> str:
    """SQL for the UTC hour (hours since 1970) of a stored time."""
    return f"(CAST(strftime('%s',{value}) AS INTEGER)/3600)"


def _span(camera: str, first: str, last: str) -> str:
    return f"DELETE FROM insight_fresh WHERE camera_id={camera} AND hour BETWEEN {hour_of(first)} AND {hour_of(f'max({first},{last})')};"


def _track_last(row: str) -> str:
    return f"COALESCE((SELECT last_seen_at FROM ai_tracks WHERE id={row}.track_id),{row}.detected_at)"


def _masked(row: str) -> str:
    return (f"EXISTS (SELECT 1 FROM insight_masks m WHERE m.camera_id={row}.camera_id AND {row}.box_x<m.x2 AND {row}.box_x+{row}.box_width>m.x1 "
            f"AND {row}.box_y<m.y2 AND {row}.box_y+{row}.box_height>m.y1)")


def _began(row: str, condition: str) -> str:
    return (f"DELETE FROM insight_fresh WHERE camera_id={row}.camera_id AND hour IN "
            f"(SELECT b.slot/4 FROM insight_begins b WHERE b.track_id={row}.track_id AND {condition});")


# Ordinary tables, not WITHOUT ROWID: the salvage (app.db_salvage, #455) copies tables by their row numbers. Each reader's index holds every
# column it reads, so a read never visits the table itself.
TABLES = (
    "CREATE TABLE IF NOT EXISTS insight_state (camera_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS insight_fresh (camera_id TEXT NOT NULL, hour INTEGER NOT NULL, state INTEGER NOT NULL, UNIQUE (camera_id,hour))",
    "CREATE INDEX IF NOT EXISTS insight_fresh_state_idx ON insight_fresh(camera_id,hour,state)",
    "CREATE TABLE IF NOT EXISTS insight_masks (camera_id TEXT NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL, x2 REAL NOT NULL, y2 REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS insight_masks_camera_idx ON insight_masks(camera_id)",
    "CREATE TABLE IF NOT EXISTS insight_begins (camera_id TEXT NOT NULL, slot INTEGER NOT NULL, track_id TEXT NOT NULL, label TEXT NOT NULL, visible_at TEXT)",
    "CREATE INDEX IF NOT EXISTS insight_begins_slot_idx ON insight_begins(camera_id,slot,label,visible_at)",
    "CREATE INDEX IF NOT EXISTS insight_begins_track_idx ON insight_begins(track_id,slot,visible_at)",
    "CREATE TABLE IF NOT EXISTS insight_events (camera_id TEXT NOT NULL, slot INTEGER NOT NULL, rule_id TEXT NOT NULL, label TEXT NOT NULL, n INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS insight_events_slot_idx ON insight_events(camera_id,slot,rule_id,label,n)",
    "CREATE TABLE IF NOT EXISTS insight_heat (camera_id TEXT NOT NULL, hour INTEGER NOT NULL, label TEXT NOT NULL, cell INTEGER NOT NULL, samples INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS insight_heat_hour_idx ON insight_heat(camera_id,hour,label,cell,samples)",
    "CREATE TABLE IF NOT EXISTS insight_heat_tracks (camera_id TEXT NOT NULL, hour INTEGER NOT NULL, label TEXT NOT NULL, track_id TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS insight_heat_tracks_hour_idx ON insight_heat_tracks(camera_id,hour,label,track_id)",
)

TRIGGERS = (
    # A detection added: its hour to the end of its track, and the hour its track began if this may be its first unmasked detection
    "CREATE TRIGGER IF NOT EXISTS insight_detection_added AFTER INSERT ON ai_detections BEGIN "
    + _span("NEW.camera_id", "NEW.detected_at", _track_last("NEW"))
    + _began("NEW", f"(b.visible_at IS NULL OR b.visible_at>NEW.detected_at) AND NOT {_masked('NEW')}")
    + " END",
    "CREATE TRIGGER IF NOT EXISTS insight_detection_removed AFTER DELETE ON ai_detections BEGIN "
    + _span("OLD.camera_id", "OLD.detected_at", _track_last("OLD"))
    + _began("OLD", "b.visible_at>=OLD.detected_at")
    + " END",
    "CREATE TRIGGER IF NOT EXISTS insight_detection_moved AFTER UPDATE OF camera_id,track_id,detected_at,box_x,box_y,box_width,box_height ON ai_detections BEGIN "
    + _span("OLD.camera_id", "OLD.detected_at", _track_last("OLD")) + _span("NEW.camera_id", "NEW.detected_at", _track_last("NEW"))
    + _began("OLD", "1") + _began("NEW", "1")
    + " END",
    # Thinning: the frames a row stands for count in its own hour's heatmap only
    "CREATE TRIGGER IF NOT EXISTS insight_detection_frames AFTER UPDATE OF stands_for ON ai_detections BEGIN "
    + _span("NEW.camera_id", "NEW.detected_at", "NEW.detected_at")
    + " END",
    "CREATE TRIGGER IF NOT EXISTS insight_track_added AFTER INSERT ON ai_tracks BEGIN "
    + _span("NEW.camera_id", "NEW.first_seen_at", "NEW.last_seen_at")
    + " END",
    "CREATE TRIGGER IF NOT EXISTS insight_track_removed AFTER DELETE ON ai_tracks BEGIN "
    + _span("OLD.camera_id", "OLD.first_seen_at", "OLD.last_seen_at")
    + " END",
    # Every detection added sets its track's first time to what it was: only a real change counts
    "CREATE TRIGGER IF NOT EXISTS insight_track_changed AFTER UPDATE OF camera_id,label,first_seen_at ON ai_tracks "
    "WHEN OLD.camera_id IS NOT NEW.camera_id OR OLD.label IS NOT NEW.label OR OLD.first_seen_at IS NOT NEW.first_seen_at BEGIN "
    + _span("OLD.camera_id", "OLD.first_seen_at", "OLD.last_seen_at") + _span("NEW.camera_id", "NEW.first_seen_at", "NEW.last_seen_at")
    + " END",
)

# Emptied after a salvage (app.db_salvage): copied totals need not agree with the rows that were saved, and are worked out again
FORGET = ("DELETE FROM insight_state", "DELETE FROM insight_fresh", "DELETE FROM insight_masks", "DELETE FROM insight_begins", "DELETE FROM insight_events",
          "DELETE FROM insight_heat", "DELETE FROM insight_heat_tracks")


def upgrade(db: sqlite3.Connection) -> None:
    for statement in (*TABLES, *TRIGGERS):
        db.execute(statement)
