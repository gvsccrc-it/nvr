"""Every row that refers to a parent the NVR deletes as a matter of course can be found through an index (#524), and the push queue is
pruned hourly without an idle write (#525)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from app import push
from app.clock import now
from app.store import connect, initialize

# Parents whose rows are deleted by a person, now and then: a full read of a child table when one goes is acceptable. A foreign key to any
# other table must have an index on its own column, because those parents are pruned by the thousand (alarms, tracks, digests, exports).
RARELY_DELETED = {"users", "cameras", "camera_groups", "cases", "console_sites", "api_tokens", "ptz_presets"}


ALARM = ("INSERT INTO alarms(id,rule_name,camera_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,state,created_at,updated_at) "
         "VALUES(?,'Rule','cam','person','zone_entry',?,'[]',?,?,'resolved','2026-11-01T00:00:00Z','2026-11-01T00:00:00Z')")


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("NVR_DB_PATH")
        os.environ["NVR_DB_PATH"] = str(Path(self.temporary.name) / "state.sqlite3")
        initialize()

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("NVR_DB_PATH", None)
        else:
            os.environ["NVR_DB_PATH"] = self.previous
        self.temporary.cleanup()


class IndexTests(Case):
    def test_the_three_lookups_are_searches_not_scans(self):
        with connect() as db:
            for table, column in (("push_deliveries", "alarm_id"), ("face_sightings", "track_id"), ("alarm_comments", "alarm_id")):
                plan = " ".join(row[3] for row in db.execute(f"EXPLAIN QUERY PLAN SELECT 1 FROM {table} WHERE {column}=?", ("x",)))
                self.assertIn("SEARCH", plan, table)
                self.assertNotIn("SCAN", plan, table)

    def test_no_foreign_key_to_a_pruned_table_is_without_an_index_on_its_own_column(self):
        missing = []
        with connect() as db:
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                leading = {db.execute(f"PRAGMA index_info('{index[1]}')").fetchone()[2] for index in db.execute(f"PRAGMA index_list('{table}')").fetchall()}
                keys = [column[1] for column in db.execute(f"PRAGMA table_info('{table}')") if column[5] == 1]
                for key in db.execute(f"PRAGMA foreign_key_list('{table}')").fetchall():
                    parent, column, on_delete = key[2], key[3], key[6]
                    if on_delete in {"CASCADE", "SET NULL"} and parent not in RARELY_DELETED and column not in leading and column not in keys:
                        missing.append(f"{table}.{column} -> {parent}")
        self.assertEqual(missing, [], "add an index on each of these columns (see m0079_cascade_indexes), or deleting a parent row reads the whole table")

    def test_deleting_alarms_does_not_read_the_whole_push_queue(self):
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('cam','Cam',X'01')")
            db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh,auth) VALUES('sub',1,'https://push.example.test/1','k','a')")
            db.executemany(ALARM, [(f"alarm-{index}", "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z", "2026-11-01T00:00:10Z") for index in range(20000)])
            db.executemany("INSERT INTO push_deliveries(id,subscription_id,alarm_id,payload_json,status,next_attempt_at,created_at) VALUES(?,?,?,?,?,?,?)",
                           [(f"delivery-{index}", "sub", f"alarm-{index}", "{}", "sent", "2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z") for index in range(20000)])
        with connect() as db:
            started = time.monotonic()
            db.execute("DELETE FROM alarms WHERE id IN (SELECT id FROM alarms LIMIT 500)")
            seconds = time.monotonic() - started
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_deliveries").fetchone()[0], 19500)
        self.assertLess(seconds, 0.5, "500 alarms: without the index this is 500 reads of the whole queue")


class PushPruneTests(Case):
    def setUp(self):
        super().setUp()
        reset = patch.object(push, "_pruned_at", float("-inf"))
        reset.start()
        self.addCleanup(reset.stop)
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('cam','Cam',X'01')")
            db.execute("INSERT INTO push_subscriptions(id,user_id,endpoint,p256dh,auth) VALUES('sub',1,'https://push.example.test/1','k','a')")

    def delivery(self, name: str, status: str, days_old: float) -> None:
        created = (now() - timedelta(days=days_old)).isoformat()
        with connect() as db:
            db.execute(ALARM, (name, created, created, created))
            db.execute("INSERT INTO push_deliveries(id,subscription_id,alarm_id,payload_json,status,next_attempt_at,created_at) VALUES(?,?,?,?,?,?,?)",
                       (name, "sub", name, "{}", status, created, created))

    def left(self) -> list[str]:
        with connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM push_deliveries ORDER BY id")]

    def test_an_idle_pass_writes_nothing(self):
        self.delivery("recent", "sent", 1)
        push.deliver_due()  # The first pass makes the server's keys
        holder = sqlite3.connect(os.environ["NVR_DB_PATH"], isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")  # Another process is writing: an idle pass must not need the lock
        try:
            with patch.object(push, "_pruned_at", float("-inf")), patch("app.store.sqlite3.connect", side_effect=lambda *args, **kwargs: real_connect(*args, **{**kwargs, "timeout": 0.2})):
                self.assertEqual(push.deliver_due(), [])
        finally:
            holder.execute("ROLLBACK")
            holder.close()

    def test_finished_deliveries_older_than_a_week_are_removed_once_an_hour(self):
        self.delivery("old-sent", "sent", 8)
        self.delivery("old-failed", "failed", 9)
        self.delivery("old-queued", "queued", 9)
        self.delivery("recent", "sent", 6)
        self.assertEqual(push.prune_deliveries(), 2)
        self.assertEqual(self.left(), ["old-queued", "recent"])
        self.delivery("another-old", "sent", 8)
        self.assertEqual(push.prune_deliveries(), 0, "not again within the hour")
        with patch.object(push, "_pruned_at", time.monotonic() - push.PRUNE_EVERY_SECONDS - 1):
            self.assertEqual(push.prune_deliveries(), 1)

    def test_the_prune_reads_by_age_through_an_index(self):
        with connect() as db:
            plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT 1 FROM push_deliveries WHERE status IN ('sent','failed') AND created_at<? LIMIT 1", ("2026",)))
        self.assertIn("push_deliveries_finished_idx", plan)


real_connect = sqlite3.connect

if __name__ == "__main__":
    unittest.main()
