"""MQTT and Home Assistant event sources keep an allowance, like HTTP sources, and plain messages no mapping takes are not stored (#528).

As decided on the ticket: 600 kept a minute from one source, the rest counted in its status; a message that is not an event and matches no
mapping is not stored, and is counted; a filter of only # (or +) is refused, and one saved before is marked and must be narrowed when next edited.
"""
from __future__ import annotations

import json
from unittest import mock

from app import external_events
from app.store import connect
from tests.test_access_grants import GrantCase

BROKER = "mqtt://door:pw@192.0.2.7:1883"


class AllowanceCase(GrantCase):
    def setUp(self):
        super().setUp()
        external_events._windows.clear()
        external_events._uncounted.clear()
        self.addCleanup(external_events._windows.clear)
        self.addCleanup(external_events._uncounted.clear)

    def source(self, topic: str = "site/doors/#", mappings=None) -> str:
        body = {"name": "Door bus", "kind": "mqtt", "url": BROKER, "topic": topic, "allow_private": True,
                "mappings": mappings if mappings is not None else [{"subject": "main-door", "camera_ids": [self.d1]}]}
        response = self.admin.post("/api/event-sources", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def stored(self, source_id: str) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM external_events WHERE source_id=?", (source_id,)).fetchone()[0]

    def view(self, source_id: str) -> dict:
        return next(item for item in self.admin.get("/api/event-sources").json()["sources"] if item["id"] == source_id)

    def event(self, number: int) -> bytes:
        return json.dumps({"event_id": f"e{number}", "type": "door_forced", "subject": "main-door"}).encode()


class AllowanceTests(AllowanceCase):
    def test_a_burst_keeps_only_the_allowance_and_counts_the_rest(self):
        source = self.source()
        for number in range(external_events.MESSAGES_PER_MINUTE + 150):
            external_events.receive_message(source, "site/doors/main", self.event(number))
        self.assertEqual(self.stored(source), external_events.MESSAGES_PER_MINUTE)
        external_events.save_counts()
        counted = self.view(source)["not_kept"]
        self.assertEqual((counted["over_allowance"], counted["unmapped"]), (150, 0))
        self.assertIsNotNone(counted["since"])

    def test_one_sources_burst_leaves_anothers_allowance_alone(self):
        busy, quiet = self.source(), self.source(topic="site/gates/#")
        for number in range(external_events.MESSAGES_PER_MINUTE + 10):
            external_events.receive_message(busy, "site/doors/main", self.event(number))
        self.assertIsNotNone(external_events.receive_message(quiet, "site/gates/main", self.event(99_999)))

    def test_home_assistants_event_topic_has_the_same_allowance(self):
        with connect() as db:
            db.execute("INSERT OR IGNORE INTO event_sources(id,name,kind,enabled,settings_json) VALUES(?,?,?,?,?)",
                       (external_events.HOME_ASSISTANT_SOURCE, "Home Assistant", "home_assistant", 1, "{}"))
        for number in range(external_events.MESSAGES_PER_MINUTE + 20):
            external_events.receive_message(external_events.HOME_ASSISTANT_SOURCE, "nvr/events/door", self.event(number))
        self.assertEqual(self.stored(external_events.HOME_ASSISTANT_SOURCE), external_events.MESSAGES_PER_MINUTE)

    def test_the_counts_are_saved_once_a_pass_not_once_a_message(self):
        source = self.source()
        for number in range(external_events.MESSAGES_PER_MINUTE + 300):
            external_events.receive_message(source, "site/doors/main", self.event(number))
        statements: list[str] = []
        real = external_events.connect

        def traced():
            db = real()
            db.set_trace_callback(statements.append)
            return db

        with mock.patch.object(external_events, "connect", traced):
            external_events.save_counts()
            external_events.save_counts()  # Nothing new: no write at all
        self.assertEqual(len([statement for statement in statements if statement.startswith("UPDATE event_sources")]), 1)


class UnmappedTests(AllowanceCase):
    def test_a_plain_message_no_mapping_takes_is_not_stored_and_is_counted(self):
        source = self.source()
        for number in range(25):
            self.assertIsNone(external_events.receive_message(source, "site/doors/hallway-light", b"ON"))
        self.assertEqual(self.stored(source), 0)
        external_events.save_counts()
        self.assertEqual(self.view(source)["not_kept"]["unmapped"], 25)

    def test_a_plain_message_a_mapping_takes_is_stored(self):
        source = self.source(mappings=[{"subject": "site/doors/bell", "camera_ids": [self.d1]}])
        self.assertIsNotNone(external_events.receive_message(source, "site/doors/bell", b"pressed"))
        self.assertEqual(self.stored(source), 1)

    def test_a_mapping_for_other_event_types_does_not_take_plain_messages(self):
        source = self.source(mappings=[{"subject": "*", "types": ["door_forced"], "camera_ids": [self.d1]}])
        self.assertIsNone(external_events.receive_message(source, "site/doors/bell", b"pressed"))
        self.assertIsNotNone(external_events.receive_message(source, "site/doors/main", self.event(1)))  # An event is kept as before
        self.assertEqual(self.stored(source), 1)


class FilterTests(AllowanceCase):
    def test_a_filter_that_takes_everything_is_refused(self):
        for topic in ("#", "+/#", "+/+", "+"):
            body = {"name": "All", "kind": "mqtt", "url": BROKER, "topic": topic, "allow_private": True}
            answer = self.admin.post("/api/event-sources", json=body)
            self.assertEqual(answer.status_code, 422, topic)
            self.assertIn("fixed level", answer.text)
        self.source(topic="site/#")  # One fixed level is enough

    def test_a_source_saved_with_one_before_is_marked_and_must_be_narrowed_but_can_be_switched_off(self):
        source = self.source()
        with connect() as db:  # As saved before the rule
            db.execute("UPDATE event_sources SET settings_json=? WHERE id=?", (json.dumps({"topic": "#", "allow_private": True}), source))
        self.assertTrue(self.view(source)["filter_takes_everything"])
        kept = self.admin.put(f"/api/event-sources/{source}", json={"name": "Door bus", "kind": "mqtt"})
        self.assertEqual(kept.status_code, 422, kept.text)
        off = self.admin.put(f"/api/event-sources/{source}", json={"name": "Door bus", "kind": "mqtt", "enabled": False})
        self.assertEqual(off.status_code, 200, off.text)
        narrowed = self.admin.put(f"/api/event-sources/{source}", json={"name": "Door bus", "kind": "mqtt", "topic": "site/doors/#", "enabled": True})
        self.assertEqual(narrowed.status_code, 200, narrowed.text)
        self.assertFalse(self.view(source)["filter_takes_everything"])
