"""The signed-footage chain stops growing for ever (#500): links of footage removed long ago are replaced by a signed checkpoint, old
signatures are thinned to one an hour, and what is kept still verifies."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import footage_chain
from app.store import connect

from tests.scene_case import SceneTestCase

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


class FootageChainPruningTests(SceneTestCase):
    def setUp(self):
        super().setUp()
        footage_chain._unreadable.clear()
        self.root = Path(self.temporary.name) / "recordings"
        (self.root / self.camera_id).mkdir()
        self.count = 0

    def record(self, when: datetime) -> int:
        """A minute of footage recorded at ``when``: its file, its row, its link and (as the worker does each minute) a signature."""
        self.count += 1
        name = f"{self.camera_id}/m{self.count}.mp4"
        (self.root / name).write_bytes(os.urandom(64))
        with connect() as db:
            segment = db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,64)",
                                 (self.camera_id, name, when.isoformat(), (when + timedelta(minutes=1)).isoformat())).lastrowid
        self.chain(when + timedelta(minutes=1))
        return segment

    def remove(self, segment: int, when: datetime) -> None:
        """Retention removes a segment at ``when``: the row goes (the trigger notes it) and the chain records the removal."""
        with connect() as db:
            path = db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (segment,)).fetchone()[0]
            db.execute("DELETE FROM recording_segments WHERE id=?", (segment,))
        (self.root / path).unlink()
        self.chain(when)

    def chain(self, when: datetime) -> None:
        with patch.object(footage_chain, "_now", lambda: when.isoformat()):
            while asyncio.run(footage_chain.append_pending()):
                pass
            footage_chain.sign_heads()

    def prune_all(self, now: datetime = NOW) -> dict[str, int]:
        total = {"links": 0, "signatures": 0}
        for _ in range(200):
            done = footage_chain.prune(now)
            if not (done["links"] or done["signatures"]):
                return total
            total = {key: total[key] + done[key] for key in total}
        self.fail("pruning never finished")

    def counts(self) -> tuple[int, int]:
        with connect() as db:
            return (db.execute("SELECT COUNT(*) FROM footage_chain").fetchone()[0], db.execute("SELECT COUNT(*) FROM footage_chain_signatures").fetchone()[0])

    def a_year_and_more(self) -> dict[str, list[int]]:
        """Footage recorded and removed two years ago, footage removed 100 days ago, and footage still on disk."""
        long_gone = [self.record(NOW - timedelta(days=760, minutes=-index)) for index in range(6)]
        for segment in long_gone:
            self.remove(segment, NOW - timedelta(days=730))
        gone = [self.record(NOW - timedelta(days=130, minutes=-index)) for index in range(4)]
        for segment in gone:
            self.remove(segment, NOW - timedelta(days=100))
        kept = [self.record(NOW - timedelta(days=5, minutes=-index)) for index in range(4)]
        return {"long_gone": long_gone, "gone": gone, "kept": kept}

    def test_links_past_the_kept_period_are_replaced_by_a_signed_checkpoint_and_the_rest_still_verifies(self):
        footage = self.a_year_and_more()
        links_before, _ = self.counts()
        self.assertEqual(links_before, 6 * 2 + 4 * 2 + 4)
        dropped = self.prune_all()
        # The twelve links of the footage removed two years ago go, all but the one the checkpoint's signature is over.
        self.assertEqual(dropped["links"], 11)
        with connect() as db:
            checkpoint = footage_chain.checkpoint(db, self.camera_id)
            first = db.execute("SELECT seq,link_hash FROM footage_chain WHERE camera_id=? ORDER BY seq LIMIT 1", (self.camera_id,)).fetchone()
            signature = db.execute("SELECT MIN(upto_seq) FROM footage_chain_signatures WHERE camera_id=?", (self.camera_id,)).fetchone()[0]
            ids = {row[0] for row in db.execute("SELECT segment_id FROM footage_chain")}
        self.assertEqual((checkpoint["seq"], checkpoint["link_hash"], checkpoint["links_removed"]), (12, first["link_hash"], 11))
        self.assertEqual((first["seq"], signature), (12, 12))
        # Footage removed within the kept period keeps both its links; footage on disk keeps its link.
        self.assertTrue(set(footage["gone"]) | set(footage["kept"]) <= ids)
        result = footage_chain.verify(self.camera_id, NOW - timedelta(days=200), NOW)
        self.assertTrue(result["valid"], result)
        self.assertEqual({entry["status"] for entry in result["segments"]}, {"ok"})
        self.assertEqual(len(result["segments"]), 4)
        self.assertFalse(result["before_kept_chain"])
        # New footage is chained and signed on from what is kept, and a second pass has nothing to do.
        newer = self.record(NOW - timedelta(minutes=10))
        self.assertEqual(self.prune_all(), {"links": 0, "signatures": 0})
        result = footage_chain.verify(self.camera_id, NOW - timedelta(days=200), NOW)
        self.assertTrue(result["valid"], result)
        self.assertIn(newer, {entry["segment_id"] for entry in result["segments"]})

    def test_a_range_older_than_the_kept_chain_says_so_and_is_not_called_tampered(self):
        self.a_year_and_more()
        self.prune_all()
        response = self.client.get(f"/api/cameras/{self.camera_id}/footage/verify",
                                   params={"start": (NOW - timedelta(days=761)).isoformat(), "end": (NOW - timedelta(days=759)).isoformat()})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result["before_kept_chain"])
        self.assertEqual(result["kept_from"], (NOW - timedelta(days=760, minutes=-6)).isoformat())  # The end of the last minute dropped
        self.assertTrue(result["valid"], result)
        self.assertIsNone(result["chain_error"])
        self.assertEqual(result["problems"], [])

    def test_footage_still_on_disk_keeps_its_link_however_old_and_holds_what_follows(self):
        held = self.record(NOW - timedelta(days=800))  # Held for a case: never removed
        old = self.record(NOW - timedelta(days=790))
        self.remove(old, NOW - timedelta(days=760))
        self.assertEqual(self.prune_all()["links"], 0)
        with connect() as db:
            self.assertIsNone(footage_chain.checkpoint(db, self.camera_id))
        result = footage_chain.verify(self.camera_id, NOW - timedelta(days=801), NOW)
        self.assertTrue(result["valid"], result)
        self.assertEqual({entry["segment_id"]: entry["status"] for entry in result["segments"]}, {held: "ok"})

    def test_an_extract_made_before_its_links_were_dropped_still_verifies(self):
        segments = [self.record(NOW - timedelta(days=760, minutes=-index)) for index in range(3)]
        with connect() as db:
            extract = footage_chain.extract(db, self.camera_id, segments)
            sources = [{"segment_id": row["segment_id"], "relative_path": row["relative_path"], "sha256": row["sha256"]}
                       for row in db.execute("SELECT * FROM footage_chain WHERE kind='segment'")]
        for segment in segments:
            self.remove(segment, NOW - timedelta(days=730))
        self.record(NOW - timedelta(days=1))
        self.assertGreater(self.prune_all()["links"], 0)
        self.assertEqual(footage_chain.verify_extract(extract, sources), {"valid": True, "error": None, "public_key": footage_chain.public_key(), "unsigned": []})

    def test_old_signatures_are_thinned_to_one_an_hour_and_the_chain_between_them_still_verifies(self):
        hour = NOW - timedelta(days=60)
        old = [self.record(hour + timedelta(minutes=index)) for index in range(5)] + [self.record(hour + timedelta(hours=1, minutes=index)) for index in range(3)]
        recent = [self.record(NOW - timedelta(days=2, minutes=-index)) for index in range(3)]
        self.assertEqual(self.counts(), (11, 11))
        dropped = self.prune_all()
        self.assertEqual(dropped, {"links": 0, "signatures": 6})  # The old ones: the last of each of the two hours stays
        with connect() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT upto_seq FROM footage_chain_signatures ORDER BY upto_seq")], [5, 8, 9, 10, 11])
        result = footage_chain.verify(self.camera_id, hour - timedelta(hours=1), NOW)
        self.assertTrue(result["valid"], result)
        self.assertEqual({entry["segment_id"] for entry in result["segments"] if entry["status"] == "ok"}, set(old + recent))
        # A link altered inside an hour's stretch is still caught by the signature at the hour's end.
        with connect() as db:
            db.execute("UPDATE footage_chain SET sha256=? WHERE seq=2", ("0" * 64,))
        self.assertFalse(footage_chain.verify(self.camera_id, hour - timedelta(hours=1), NOW)["valid"])

    def test_a_rewritten_checkpoint_link_fails_and_is_never_dropped_from(self):
        self.a_year_and_more()
        self.prune_all()
        with connect() as db:
            db.execute("UPDATE footage_chain SET sha256=? WHERE seq=12", ("0" * 64,))
        result = footage_chain.verify(self.camera_id, NOW - timedelta(days=200), NOW)
        self.assertFalse(result["valid"])
        self.assertIsNotNone(result["chain_error"])

    def test_a_step_is_bounded_and_reads_before_it_writes(self):
        self.a_year_and_more()
        with patch.object(footage_chain, "PRUNE_BATCH", 4):
            first = footage_chain.prune(NOW)
            self.assertEqual(first["links"], 4)
            # Between steps (some links before the checkpoint not dropped yet) the kept chain verifies all the same.
            self.assertTrue(footage_chain.verify(self.camera_id, NOW - timedelta(days=800), NOW)["valid"])
            self.assertEqual(self.prune_all()["links"], 7)
        # With nothing to drop, no statement that writes is run at all.
        statements: list[str] = []
        real = footage_chain.connect

        class Watched:
            def __enter__(self_inner):
                self_inner.context = real()
                db = self_inner.context.__enter__()
                db.set_trace_callback(statements.append)
                return db

            def __exit__(self_inner, *args):
                return self_inner.context.__exit__(*args)

        with patch.object(footage_chain, "connect", Watched):
            self.assertEqual(footage_chain.prune(NOW), {"links": 0, "signatures": 0})
        self.assertTrue(statements)
        self.assertFalse([statement for statement in statements if statement.lstrip().split()[0].upper() in {"BEGIN", "DELETE", "INSERT", "UPDATE"}], statements)

    def test_the_kept_period_is_a_setting_within_ninety_days_and_ten_years(self):
        for value, days in (("", 365), ("30", 90), ("120", 120), ("99999", 3650), ("soon", 365)):
            with patch.dict(os.environ, {"NVR_FOOTAGE_CHAIN_KEEP_DAYS": value} if value else {}, clear=False):
                if not value:
                    os.environ.pop("NVR_FOOTAGE_CHAIN_KEEP_DAYS", None)
                self.assertEqual(footage_chain.keep_days(), days)
        footage = self.a_year_and_more()
        with patch.dict(os.environ, {"NVR_FOOTAGE_CHAIN_KEEP_DAYS": "90"}):
            self.prune_all()
        with connect() as db:  # Removed 100 days ago: past a 90-day period
            self.assertFalse(db.execute(f"SELECT 1 FROM footage_chain WHERE segment_id IN ({','.join(map(str, footage['gone'][:3]))})").fetchone())
        self.assertTrue(footage_chain.verify(self.camera_id, NOW - timedelta(days=200), NOW)["valid"])
