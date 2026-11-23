"""Salvaging a damaged database keeps every row that still reads and fills only the lost ones from an older copy (#455)."""
from __future__ import annotations

import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from app import db_health, db_salvage

_ENV = ("NVR_DB_PATH", "NVR_CONTROL_DIR")
ROWS = 6000


def digest(path: Path, table: str, where: str = "") -> list[tuple]:
    db = sqlite3.connect(path)
    try:
        return [tuple(row) for row in db.execute(f"SELECT rowid,* FROM {table} {where} ORDER BY rowid")]
    finally:
        db.close()


class SalvageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        self.path = self.root / "nvr.sqlite3"
        os.environ["NVR_DB_PATH"] = str(self.path)
        os.environ["NVR_CONTROL_DIR"] = str(self.root / "run")
        from app.store import initialize

        initialize()
        db = sqlite3.connect(self.path)  # A plain connection: foreign keys are off, so the rows need no camera
        db.executemany("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,starts_at,checked_at) VALUES(?,'covered',?,?,?)",
                       [(f"cam{index % 7}", f"row {index} " + "x" * 150, "2026-11-19T00:00:00Z", "2026-11-19T00:00:00Z") for index in range(ROWS)])
        db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        self.copy = self.root / "nvr.sqlite3.pre-migration-v1-to-v2-20261119T221000000000Z"
        shutil.copyfile(self.path, self.copy)
        db = sqlite3.connect(self.path)  # Two days of changes since the copy
        db.execute("UPDATE coverage_intervals SET checked_at='2026-11-21T03:39:00Z'")
        db.executemany("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,starts_at) VALUES('cam1','covered',?,'2026-11-21T00:00:00Z')", [("new " + "y" * 150,) for _ in range(500)])
        db.execute("UPDATE users SET username='renamed'")
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        self.before = digest(self.path, "coverage_intervals")
        self.output = self.root / "salvaged.sqlite3"

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def zero_table_pages(self, count: int) -> None:
        """Turn ``count`` leaf pages of the coverage table to zeros, apart from each other, as the virtual disk did."""
        data = self.path.read_bytes()
        leaves = [number for number in range(2, len(data) // 4096)
                  if data[number * 4096] == 0x0D and b"x" * 150 in data[number * 4096:(number + 1) * 4096]]  # A table leaf holding this test's rows
        chosen = leaves[10::max(1, (len(leaves) - 20) // count)][:count]
        self.assertEqual(len(chosen), count)
        with self.path.open("r+b") as handle:
            for number in chosen:
                handle.seek(number * 4096)
                handle.write(bytes(4096))

    def test_the_alarm_counts_are_worked_out_again_from_the_salvaged_alarms(self):
        db = sqlite3.connect(self.path)  # Foreign keys off: the alarms need no camera or rule
        db.executemany("INSERT INTO alarms(id,rule_id,rule_name,camera_id,track_id,label,condition_type,occurred_at,box_json,clip_starts_at,clip_ends_at,state,"
                       "created_at,updated_at) VALUES(?,?,'Gate',?,'t','person','zone_entry','2026-11-21T00:00:00Z','{}','2026-11-21T00:00:00Z',"
                       "'2026-11-21T00:00:10Z',?,'2026-11-21T00:00:00Z','2026-11-21T00:00:00Z')",
                       [(f"a{index}", "gate" if index % 2 else None, f"cam{index % 3}", "resolved" if index % 4 else "new") for index in range(40)])
        db.execute("UPDATE alarm_counts SET count=count+5")  # Counts that no longer agree with the alarms, as copied counts may not
        db.commit()
        db.close()
        db_salvage.salvage(self.path, self.output)
        salvaged = sqlite3.connect(self.output)
        try:
            self.assertEqual(
                salvaged.execute("SELECT camera_id,rule_key,rule_name,state,count FROM alarm_counts ORDER BY 1,2,3,4").fetchall(),
                salvaged.execute("SELECT camera_id,COALESCE(rule_id,''),rule_name,state,COUNT(*) FROM alarms GROUP BY 1,2,3,4 ORDER BY 1,2,3,4").fetchall())
            self.assertTrue(salvaged.execute("SELECT 1 FROM alarm_counts").fetchone())
        finally:
            salvaged.close()

    def test_a_healthy_file_is_copied_whole(self):
        results = db_salvage.salvage(self.path, self.output)
        self.assertTrue(all(result.whole for result in results))
        self.assertEqual(digest(self.output, "coverage_intervals"), self.before)
        self.assertEqual(digest(self.output, "users"), digest(self.path, "users"))
        source, salvaged = sqlite3.connect(self.path), sqlite3.connect(self.output)
        try:
            schema = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            self.assertEqual(salvaged.execute(schema).fetchall(), source.execute(schema).fetchall(), "tables, indexes, triggers and views")
            self.assertEqual(salvaged.execute("SELECT name,seq FROM sqlite_sequence ORDER BY name").fetchall(), source.execute("SELECT name,seq FROM sqlite_sequence ORDER BY name").fetchall())
        finally:
            source.close()
            salvaged.close()
        self.assertTrue(db_health.check(full=True, path=self.output).ok)

    def test_lost_pages_cost_only_their_rows_and_those_come_from_the_copy(self):
        self.zero_table_pages(3)
        damaged = self.path.read_bytes()
        copied = self.copy.read_bytes()
        results = {result.name: result for result in db_salvage.salvage(self.path, self.output, self.copy)}
        self.assertEqual((self.path.read_bytes(), self.copy.read_bytes()), (damaged, copied), "neither source was written to")
        coverage = results["coverage_intervals"]
        self.assertFalse(coverage.whole)
        self.assertEqual(len(coverage.lost), 3)
        self.assertTrue(all(result.whole for name, result in results.items() if name != "coverage_intervals"))
        after = digest(self.output, "coverage_intervals")
        self.assertEqual([row[0] for row in after], [row[0] for row in self.before], "every row is there")
        self.assertEqual(coverage.read + coverage.filled, len(self.before))
        self.assertLess(coverage.filled, 100, "three pages hold a few dozen rows")
        self.assertGreater(coverage.filled, 0)
        stale = [row for row, was in zip(after, self.before) if row != was]
        self.assertEqual(len(stale), coverage.filled, "only the filled rows differ: they are as the copy had them")
        self.assertTrue(all(row[-1] == "2026-11-19T00:00:00Z" for row in stale))
        self.assertEqual(digest(self.output, "users")[0][2], "renamed", "an undamaged table keeps what was written after the copy")
        self.assertTrue(db_health.check(full=True, path=self.output).ok)
        salvaged = sqlite3.connect(self.output)
        try:
            self.assertEqual(salvaged.execute("SELECT COUNT(*) FROM coverage_intervals INDEXED BY coverage_camera_time_idx WHERE camera_id='cam1'").fetchone()[0],
                             len([row for row in self.before if row[2] == "cam1"]), "the index was rebuilt from the rows")
            salvaged.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,starts_at) VALUES('cam1','covered','after','2026-11-22T00:00:00Z')")
            self.assertEqual(salvaged.execute("SELECT MAX(id) FROM coverage_intervals").fetchone()[0], self.before[-1][0] + 1, "row numbers carry on")
        finally:
            salvaged.close()

    def test_without_a_copy_the_readable_rows_are_kept_and_the_loss_is_counted(self):
        self.zero_table_pages(1)
        results = {result.name: result for result in db_salvage.salvage(self.path, self.output)}
        coverage = results["coverage_intervals"]
        self.assertEqual(coverage.filled, 0)
        self.assertEqual(len(digest(self.output, "coverage_intervals")), coverage.read)
        self.assertLess(len(self.before) - coverage.read, 40)
        self.assertGreater(len(self.before) - coverage.read, 0)
        self.assertIn("those rows are lost", db_salvage.report(list(results.values()), filled=False))

    def test_rows_lost_at_the_very_end_of_a_table_are_handled(self):
        with patch.object(db_salvage._Reader, "after", autospec=True, side_effect=lambda reader, rowid, limit: None if reader.table == "coverage_intervals" and rowid > 0 and rowid + limit > 6400 else real(reader, rowid, limit)):
            results = {result.name: result for result in db_salvage.salvage(self.path, self.output, self.copy)}
        coverage = results["coverage_intervals"]
        self.assertEqual(coverage.lost, [(6400, None)])
        self.assertEqual(coverage.read, 6400)
        self.assertEqual(coverage.filled, 0, "the copy is older than those rows")

    def test_the_command_refuses_an_output_that_exists_and_reports_in_plain_words(self):
        self.zero_table_pages(1)
        self.output.write_bytes(b"something else")
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(db_salvage.main([str(self.path), str(self.output)]), 1)
        self.assertIn("already exists", errors.getvalue())
        self.assertEqual(self.output.read_bytes(), b"something else")
        self.output.unlink()
        printed = io.StringIO()
        with redirect_stdout(printed):
            self.assertEqual(db_salvage.main([str(self.path), str(self.output), "--fill-from", str(self.copy)]), 0)
        self.assertRegex(printed.getvalue(), r"coverage_intervals: \d+ rows read from the damaged file, 1 stretch\(es\) could not be read, \d+ rows taken from the healthy copy")
        self.assertIn("passed its integrity check", printed.getvalue())

    def test_a_file_whose_table_list_is_lost_is_refused_and_nothing_is_left_behind(self):
        with self.path.open("r+b") as handle:
            handle.seek(100)
            handle.write(bytes(3996))  # The first page, after the file header
        with self.assertRaisesRegex(db_salvage.SalvageError, "cannot be salvaged this way"):
            db_salvage.salvage(self.path, self.output)
        self.assertFalse(self.output.exists())


real = db_salvage._Reader.after

if __name__ == "__main__":
    unittest.main()
