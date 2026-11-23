"""Plate, attribute, natural-language and "find similar" searches leave a record of who searched for what (#519).

As decided on the ticket: all four are logged in the security log with the person, the kind, the query, the cameras and the range, never a
picture or an embedding; the same person repeating the same query within a minute adds no row; the write does not hold up the search.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import search_audit
from app.store import connect
from tests.test_access_grants import GrantCase

NOW = datetime.now(timezone.utc).replace(microsecond=0)
RANGE = {"start": (NOW - timedelta(days=1)).isoformat(), "end": NOW.isoformat()}


class SearchAuditTests(GrantCase):
    def rows(self) -> list[dict]:
        search_audit.flush()
        with connect() as db:
            return [{**dict(row), "details": json.loads(row["details_json"])}
                    for row in db.execute("SELECT actor_id,action,resource_type,details_json FROM audit_log WHERE action LIKE 'search\\_%' ESCAPE '\\' ORDER BY id")]

    def operator_id(self) -> int:
        return self.limited_id

    def test_a_plate_search_records_the_person_the_pattern_the_cameras_and_the_range(self):
        answer = self.limited.get("/api/plates/search", params={"q": "AB12*", **RANGE})
        self.assertEqual(answer.status_code, 200, answer.text)
        [row] = self.rows()
        self.assertEqual((row["actor_id"], row["action"], row["resource_type"]), (self.limited_id, "search_plate", "search"))
        self.assertEqual(row["details"]["pattern"], "AB12*")
        self.assertEqual(row["details"]["cameras"], sorted([self.d1, self.d2]))  # Only the cameras they may see: never the others' names
        self.assertEqual((row["details"]["start"], row["details"]["end"]), (RANGE["start"], RANGE["end"]))

    def test_an_attribute_search_records_its_filters(self):
        answer = self.limited.get("/api/ai/search", params={"label": "vehicle", "vehicle_colour": "red", **RANGE})
        self.assertEqual(answer.status_code, 200, answer.text)
        [row] = self.rows()
        self.assertEqual(row["action"], "search_attributes")
        self.assertEqual((row["details"]["label"], row["details"]["vehicle_colour"]), ("vehicle", "red"))

    def test_a_natural_language_search_records_the_words_and_no_embedding(self):
        answer = self.limited.get("/api/ai/search/ask", params={"q": "white van at the gate", **RANGE})
        self.assertEqual(answer.status_code, 200, answer.text)
        [row] = self.rows()
        self.assertEqual((row["action"], row["details"]["words"]), ("search_words", "white van at the gate"))
        self.assertEqual(set(row["details"]), {"words", "cameras", "start", "end"})

    def test_a_similar_search_records_the_object_it_started_from(self):
        with connect() as db:
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES('t1',?,'person',?,?,'m','1','CPU')",
                       (self.d1, (NOW - timedelta(hours=1)).isoformat(), NOW.isoformat()))
        with mock.patch("app.similar.find_similar", return_value=[]):
            answer = self.limited.get("/api/ai/tracks/t1/similar", params=RANGE)
        self.assertEqual(answer.status_code, 200, answer.text)
        [row] = self.rows()
        self.assertEqual((row["action"], row["details"]["track_id"]), ("search_similar", "t1"))

    def test_no_picture_or_embedding_is_in_any_row(self):
        self.limited.get("/api/plates/search", params={"q": "AB12", **RANGE})
        self.limited.get("/api/ai/search", params={"label": "person", **RANGE})
        self.limited.get("/api/ai/search/ask", params={"q": "a person in red", **RANGE})
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        for row in rows:
            text = json.dumps(row["details"]).lower()
            for word in ("embedding", "vector", "jpeg", "picture", "crop", "thumbnail"):
                self.assertNotIn(word, text)

    def test_the_same_query_within_a_minute_adds_no_row_and_another_query_does(self):
        for _ in range(5):
            self.limited.get("/api/plates/search", params={"q": "AB12", **RANGE})
        self.assertEqual(len(self.rows()), 1)
        self.limited.get("/api/plates/search", params={"q": "CD34", **RANGE})
        self.full.get("/api/plates/search", params={"q": "AB12", **RANGE})  # Someone else asking the same is their own row
        self.assertEqual(len(self.rows()), 3)
        later = datetime.now(timezone.utc) + timedelta(minutes=2)
        query = self.rows()[0]["details"]
        self.assertTrue(search_audit.write(self.limited_id, "plate", query, now=later))  # A minute on, the same query is a new row

    def test_a_refused_search_records_nothing(self):
        self.assertEqual(self.limited.get("/api/plates/search", params={"q": "!!", **RANGE}).status_code, 422)
        self.assertEqual(self.rows(), [])

    def test_the_search_does_not_wait_for_the_write(self):
        import threading

        writing = threading.Event()
        release = threading.Event()
        real = search_audit.write

        def slow(*args, **kwargs):
            writing.set()
            release.wait(10)  # The database's write lock held elsewhere, say
            return real(*args, **kwargs)

        with mock.patch.object(search_audit, "write", slow):
            answer = self.limited.get("/api/plates/search", params={"q": "AB12", **RANGE})
            self.assertEqual(answer.status_code, 200)  # Answered while the write still waits
            self.assertTrue(writing.wait(5))
            release.set()
            search_audit.flush()
        self.assertEqual(len(self.rows()), 1)
