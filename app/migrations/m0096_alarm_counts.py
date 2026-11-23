"""The alarm inbox's counts and rule filter are kept up with the alarms, not worked out from them on every poll (#517).

Every open tab asks for the inbox every five seconds. Its counts (alarms in each state) and its rule filter (every rule that raised an
alarm) were read from every alarm, resolved history included: about 15 ms a poll at 200,000 alarms for an administrator, and for an
operator limited to some cameras about 300 ms, as every alarm of their cameras was read and grouped. This table holds how many alarms
there are of each camera, rule, rule name and state, kept by triggers, so each answer reads a few rows.
"""
from __future__ import annotations

import sqlite3

NAME = "alarm_counts"

KEY_OLD = "camera_id=OLD.camera_id AND rule_key=COALESCE(OLD.rule_id,'') AND rule_name=OLD.rule_name AND state=OLD.state"
ADD_NEW = ("INSERT INTO alarm_counts(camera_id,rule_key,rule_name,state,count) VALUES(NEW.camera_id,COALESCE(NEW.rule_id,''),NEW.rule_name,NEW.state,1) "
           "ON CONFLICT(camera_id,rule_key,rule_name,state) DO UPDATE SET count=count+1;")
# Work the table out again from the alarms: here, and after a salvage, whose copied counts need not agree with the alarms it kept
RECOUNT = ("DELETE FROM alarm_counts",
           "INSERT INTO alarm_counts(camera_id,rule_key,rule_name,state,count) "
           "SELECT camera_id,COALESCE(rule_id,''),rule_name,state,COUNT(*) FROM alarms GROUP BY camera_id,COALESCE(rule_id,''),rule_name,state")
TAKE_OLD = f"UPDATE alarm_counts SET count=count-1 WHERE {KEY_OLD}; DELETE FROM alarm_counts WHERE {KEY_OLD} AND count<=0;"


def upgrade(db: sqlite3.Connection) -> None:
    # rule_key is the rule's id, or '' for an alarm whose rule was removed (a key column cannot be NULL and still be unique)
    # An ordinary table with a unique key, not WITHOUT ROWID: the salvage (app.db_salvage, #455) copies tables by their row numbers
    db.execute("CREATE TABLE IF NOT EXISTS alarm_counts (camera_id TEXT NOT NULL, rule_key TEXT NOT NULL, rule_name TEXT NOT NULL, state TEXT NOT NULL, "
               "count INTEGER NOT NULL, UNIQUE (camera_id,rule_key,rule_name,state))")
    db.execute("CREATE INDEX IF NOT EXISTS alarm_counts_rule_idx ON alarm_counts(rule_key)")
    for statement in RECOUNT:
        db.execute(statement)
    db.execute(f"CREATE TRIGGER IF NOT EXISTS alarm_counts_insert AFTER INSERT ON alarms BEGIN {ADD_NEW} END")
    db.execute(f"CREATE TRIGGER IF NOT EXISTS alarm_counts_delete AFTER DELETE ON alarms BEGIN {TAKE_OLD} END")
    # Also fired by the foreign keys' own changes: a removed rule sets rule_id to NULL, a removed camera deletes its alarms
    db.execute("CREATE TRIGGER IF NOT EXISTS alarm_counts_update AFTER UPDATE OF camera_id,rule_id,rule_name,state ON alarms "
               "WHEN OLD.camera_id IS NOT NEW.camera_id OR OLD.rule_id IS NOT NEW.rule_id OR OLD.rule_name IS NOT NEW.rule_name OR OLD.state IS NOT NEW.state "
               f"BEGIN {TAKE_OLD} {ADD_NEW} END")
