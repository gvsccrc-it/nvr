"""A shared live view can be marked to keep streaming while its page is hidden (#533).

A Live page hidden for 2 minutes gives its streams back (``web/src/features/live/hiddenPause.ts``); an administrator can mark a shared view
"Keep streaming when hidden" for a control-room screen. Off by default, set per view (not per person), and audited."""
from __future__ import annotations

import json
import unittest

from app.migrations import MIGRATIONS
from app.store import connect
from tests.test_saved_views import SavedViewsTests
from tests.test_support import principal


class KeepStreamingTests(unittest.TestCase):
    setUp = SavedViewsTests.setUp  # The same database, cameras and people; not its tests
    tearDown = SavedViewsTests.tearDown

    def shared(self, name: str = "Control room") -> None:
        self.active_user = principal(3, "admin", "administrator")
        made = self.client.put(f"/api/live/shared-layouts/{name}", json={"camera_ids": self.camera_ids[:2], "grid_size": 4})
        self.assertEqual(made.status_code, 200, made.text)

    def flag(self, name: str = "Control room") -> bool | None:
        views = self.client.get("/api/live/views").json()["shared_views"]
        return next(view.get("keep_streaming") for view in views if view["name"] == name)

    def test_a_shared_view_is_off_by_default_and_an_administrator_can_turn_it_on_and_off(self):
        self.shared()
        self.assertIs(self.flag(), False)
        for value in (True, False, True):
            answer = self.client.put("/api/live/shared-layouts/Control%20room/keep-streaming", json={"keep_streaming": value})
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertIs(self.flag(), value)
        # Per view, not per person: an operator opening the view sees the administrator's setting
        self.active_user = principal(1, "operator", "operator")
        self.assertIs(self.flag(), True)

    def test_saving_the_view_again_keeps_the_setting(self):
        self.shared()
        self.client.put("/api/live/shared-layouts/Control%20room/keep-streaming", json={"keep_streaming": True})
        self.shared()
        self.assertIs(self.flag(), True)

    def test_only_an_administrator_may_change_it_and_an_unknown_view_is_not_found(self):
        self.shared()
        self.active_user = principal(1, "operator", "operator")
        refused = self.client.put("/api/live/shared-layouts/Control%20room/keep-streaming", json={"keep_streaming": True})
        self.assertEqual(refused.status_code, 403)
        self.assertIs(self.flag(), False)
        self.active_user = principal(3, "admin", "administrator")
        self.assertEqual(self.client.put("/api/live/shared-layouts/Nowhere/keep-streaming", json={"keep_streaming": True}).status_code, 404)
        self.assertEqual(self.client.put("/api/live/shared-layouts/Control%20room/keep-streaming", json={"keep_streaming": "maybe"}).status_code, 422)

    def test_a_change_is_audited(self):
        self.shared()
        self.client.put("/api/live/shared-layouts/Control%20room/keep-streaming", json={"keep_streaming": True})
        with connect() as db:
            rows = db.execute("SELECT actor_id,resource_id,details_json FROM audit_log WHERE action='shared_view_keep_streaming_changed'").fetchall()
        self.assertEqual([(row["actor_id"], row["resource_id"], json.loads(row["details_json"])) for row in rows], [(3, "Control room", {"keep_streaming": True})])

    def test_personal_views_do_not_carry_the_setting(self):
        self.client.put("/api/live/layouts/Mine", json={"camera_ids": self.camera_ids[:1], "grid_size": 1})
        self.assertNotIn("keep_streaming", self.client.get("/api/live/views").json()["layouts"][0])

    def test_the_migration_is_registered_and_the_column_refuses_other_values(self):
        self.assertIn(106, [version for version, _ in MIGRATIONS])
        self.shared()
        with connect() as db, self.assertRaises(Exception):
            db.execute("UPDATE shared_viewer_layouts SET keep_streaming=2")


if __name__ == "__main__":
    unittest.main()
