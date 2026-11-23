"""The people an alarm can be assigned to are not a list of every account (#521).

The route listed the id and name of every account to anyone signed in and to a read token, administrators and disabled accounts included:
the names a guesser would try, and those whose lock-out would hurt most.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.store import connect
from tests.test_access_grants import GrantCase


class AssigneeTests(GrantCase):
    def names(self, client: TestClient, **params) -> list[str]:
        response = client.get("/api/alarms/assignees", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return [user["username"] for user in response.json()["users"]]

    def test_without_a_camera_an_operator_is_told_only_about_themselves(self):
        self.assertEqual(self.names(self.limited), ["limited"])
        self.assertEqual(self.names(self.limited, camera_id=""), ["limited"])
        self.assertEqual(self.names(self.full), ["full"])

    def test_with_a_camera_they_see_the_enabled_people_who_can_see_it(self):
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role,disabled_at) VALUES('gone','x','administrator','2026-11-01T00:00:00Z')")
        self.grant(self.full_id, [{"scope_type": "all"}])
        self.assertEqual(self.names(self.limited, camera_id=self.d1), ["full", "keeper", "limited"])  # Not the disabled administrator
        self.assertEqual(self.names(self.full, camera_id=self.y1), ["full", "keeper"])  # Not the operator who cannot see the Yard

    def test_a_camera_they_cannot_see_is_answered_as_one_that_does_not_exist(self):
        hidden = self.limited.get("/api/alarms/assignees", params={"camera_id": self.y1})
        missing = self.limited.get("/api/alarms/assignees", params={"camera_id": "no-such-camera"})
        self.assertEqual((hidden.status_code, hidden.json()), (missing.status_code, missing.json()))
        self.assertEqual(hidden.status_code, 404)
        self.assertNotIn("keeper", hidden.text)

    def test_a_read_token_is_told_only_about_its_own_account_without_a_camera(self):
        created = self.admin.post("/api/admin/api-tokens", json={"name": "feed", "scopes": ["read"]})
        self.assertEqual(created.status_code, 201, created.text)
        client = TestClient(self.app)
        client.headers["Authorization"] = f"Bearer {created.json()['secret']}"
        self.assertEqual(self.names(client), ["keeper"])
        self.assertEqual(self.names(client, camera_id=self.d1), ["full", "keeper", "limited"])  # "full" sees every camera in this fixture

    def test_assigning_from_the_list_still_works(self):
        alarm = self.add_alarm(self.d1)
        users = self.limited.get("/api/alarms/assignees", params={"camera_id": self.d1}).json()["users"]
        target = next(user for user in users if user["username"] == "keeper")
        self.assertEqual(self.limited.post(f"/api/alarms/{alarm}/assign", json={"user_id": target["id"]}).status_code, 200)
