"""Listing a camera's recordings for a range of dates is bounded and paged (#497)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.api import footage
from app.store import connect
from tests import test_access_grants

START = datetime(2026, 10, 1, tzinfo=timezone.utc)


class SegmentListingTests(test_access_grants.GrantCase):
    def footage(self, count: int) -> list[int]:
        with connect() as db:
            db.execute(f"""
                WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i < {count - 1})
                INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes)
                SELECT ?, ? || '/' || i || '.mp4', strftime('%Y-%m-%dT%H:%M:%S+00:00', ?, '+' || i || ' minutes'),
                       strftime('%Y-%m-%dT%H:%M:%S+00:00', ?, '+' || (i + 1) || ' minutes'), 1000 FROM n""",
                       (self.d1, self.d1, START.isoformat(), START.isoformat()))
            return [row[0] for row in db.execute("SELECT id FROM recording_segments WHERE camera_id=? ORDER BY starts_at,id", (self.d1,))]

    def listing(self, **params) -> dict:
        answer = self.admin.get(f"/api/cameras/{self.d1}/segments", params=params)
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()

    def test_ten_years_of_a_camera_with_5000_recordings_comes_a_page_at_a_time_and_every_one_once(self):
        ids = self.footage(5000)
        ten_years = {"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z"}
        first = self.listing(**ten_years)
        self.assertEqual(len(first["segments"]), footage.SEGMENT_PAGE)
        self.assertIsNotNone(first["next"])
        seen, page, pages = [segment["id"] for segment in first["segments"]], first, 1
        while page["next"]:
            page = self.listing(**ten_years, after=page["next"])
            self.assertLessEqual(len(page["segments"]), footage.SEGMENT_PAGE)
            seen += [segment["id"] for segment in page["segments"]]
            pages += 1
        self.assertEqual(seen, ids)  # In order, none twice, none missed
        self.assertEqual(pages, 5)

    def test_a_short_range_is_one_answer_with_no_next(self):
        self.footage(50)
        answer = self.listing(**{"from": START.isoformat(), "to": (START + timedelta(hours=2)).isoformat()})
        self.assertEqual((len(answer["segments"]), answer["next"]), (50, None))

    def test_holds_come_in_one_query_for_the_page_and_say_the_same_as_before(self):
        ids = self.footage(300)
        with connect() as db:
            for segment_id, expires in ((ids[3], None), (ids[7], "2099-01-01T00:00:00.000Z"), (ids[9], "2001-01-01T00:00:00.000Z")):
                db.execute("INSERT INTO recording_holds(id,segment_id,camera_id,created_by,created_at,expires_at) VALUES(?,?,?,1,'2000-06-01T00:00:00.000Z',?)",
                           (str(uuid.uuid4()), segment_id, self.d1, expires))
            db.execute("INSERT INTO recording_holds(id,segment_id,camera_id,created_by,released_at) VALUES(?,?,?,1,'2026-10-02T00:00:00.000Z')", (str(uuid.uuid4()), ids[11], self.d1))
        real = footage.connect
        seen: list[str] = []

        def traced():
            db = real()
            db.set_trace_callback(seen.append)
            return db

        from unittest import mock
        with mock.patch.object(footage, "connect", traced):
            answer = self.listing(**{"from": START.isoformat(), "to": (START + timedelta(days=1)).isoformat()})
        # One query for the page's holds, and none inside the query for the recordings (two there were run once a row)
        self.assertEqual(len([statement for statement in seen if "recording_holds" in statement]), 1)
        self.assertFalse([statement for statement in seen if "FROM recording_segments" in statement and "recording_holds" in statement])
        by_id = {segment["id"]: segment for segment in answer["segments"]}
        self.assertEqual((by_id[ids[3]]["preserved"], by_id[ids[3]]["hold_expires_at"]), (True, None))
        self.assertEqual((by_id[ids[7]]["preserved"], by_id[ids[7]]["hold_expires_at"]), (True, "2099-01-01T00:00:00.000Z"))
        self.assertFalse(by_id[ids[9]]["preserved"])  # Expired
        self.assertFalse(by_id[ids[11]]["preserved"])  # Released
        self.assertEqual(sum(segment["preserved"] for segment in answer["segments"]), 2)

    def test_the_interfaces_own_listing_is_unchanged_and_a_bad_cursor_is_refused(self):
        self.footage(700)
        newest = self.listing()
        self.assertEqual(len(newest["segments"]), 500)
        self.assertGreater(newest["segments"][0]["starts_at"], newest["segments"][-1]["starts_at"])  # Newest first, as before
        self.assertIsNone(newest["next"])
        for bad in ("nonsense", "2026-10-01T00:00:00+00:00|x"):
            self.assertEqual(self.admin.get(f"/api/cameras/{self.d1}/segments", params={"from": "2020-01-01T00:00:00Z", "to": "2030-01-01T00:00:00Z", "after": bad}).status_code, 422)
        self.assertEqual(self.admin.get(f"/api/cameras/{self.d1}/segments", params={"after": "2026-10-01T00:00:00+00:00|1"}).status_code, 422)


if __name__ == "__main__":
    import unittest

    unittest.main()
