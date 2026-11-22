"""An API token cannot change how people sign in, nor the record of what was done (#463, the decision on the ticket)."""
from __future__ import annotations

import unittest

from fastapi.routing import APIRoute

from app import principal
from app.routes import all_routes
from app.store import connect
from tests import test_bootstrap
from tests.test_support import TEST_PASSWORD, setup_code

# Every route that needs an administrator signed in with a browser. A new route of this kind is added here on purpose; a route that
# changes sign-in, accounts, the security log's keeping or access grants and is NOT here is the mistake this list exists to catch.
SESSION_ONLY = {
    ("PUT", "/api/admin/security"),
    ("DELETE", "/api/admin/users/{user_id}/mfa"),
    ("PUT", "/api/admin/users/{user_id}/disabled"),
    ("PUT", "/api/admin/security-log/settings"),
    ("PUT", "/api/admin/access-grants"),
    ("DELETE", "/api/admin/access-grants/{grant_id}"),
}
# Refused to tokens by a check of their own (app/api/automation.py)
TOKEN_MANAGEMENT = {("GET", "/api/admin/api-tokens"), ("POST", "/api/admin/api-tokens"), ("DELETE", "/api/admin/api-tokens/{token_id}")}


class SessionOnlyTests(unittest.TestCase):
    setUp = test_bootstrap.BootstrapTests.setUp
    tearDown = test_bootstrap.BootstrapTests.tearDown

    def routes(self) -> list[APIRoute]:
        return [route for route in all_routes(self.client.app) if isinstance(route, APIRoute)]

    def signed_in(self) -> None:
        created = self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD, "setup_code": setup_code()})
        self.assertEqual(created.status_code, 201, created.text)

    def test_the_session_only_routes_are_exactly_these(self):
        found = {(method, route.path) for route in self.routes() for method in route.methods - {"HEAD", "OPTIONS"}
                 if principal.depends_on(route.dependant, principal.administrator_session)}
        self.assertEqual(found, SESSION_ONLY)
        # Every route that writes under these paths is one of them: a new way to change the same things cannot be added beside them unnoticed
        guarded = ("/api/admin/security", "/api/admin/users/", "/api/admin/security-log", "/api/admin/access-grants", "/api/admin/api-tokens")
        writers = {(method, route.path) for route in self.routes() for method in route.methods - {"GET", "HEAD", "OPTIONS"} if route.path.startswith(guarded)}
        self.assertEqual(writers - SESSION_ONLY - TOKEN_MANAGEMENT, set(), "a route that changes sign-in, accounts, the log or grants is open to API tokens")

    def test_no_administrator_route_opens_to_a_scope_weaker_than_admin(self):
        for route in self.routes():
            if not principal.depends_on(route.dependant, principal.administrator):
                continue
            named = getattr(route.endpoint, "required_scope", None)
            self.assertIn(named, (None, "admin"), f"{route.path} is an administrator's route and names the scope {named}")
            for method in route.methods:
                self.assertEqual(principal.scope_for(route, method), "admin", route.path)

    def test_an_administrators_route_that_named_a_weaker_scope_would_still_need_admin(self):
        route = next(route for route in self.routes() if route.path == "/api/admin/security" and "GET" in route.methods)
        route.endpoint.required_scope = "media"
        try:
            self.assertEqual(principal.scope_for(route, "GET"), "admin")
        finally:
            del route.endpoint.required_scope

    def test_an_admin_token_is_refused_each_of_them_and_a_browser_session_is_not(self):
        self.signed_in()
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('other','x','operator')")
            other = db.execute("SELECT id FROM users WHERE username='other'").fetchone()[0]
        secret = self.client.post("/api/admin/api-tokens", json={"name": "Automation", "scopes": ["admin"]}).json()["secret"]
        token = {"Authorization": f"Bearer {secret}"}
        calls = [
            ("PUT", "/api/admin/security", {"mfa_policy": "off", "remote_mfa": False}),
            ("DELETE", f"/api/admin/users/{other}/mfa", None),
            ("PUT", f"/api/admin/users/{other}/disabled", {"disabled": True}),
            ("PUT", "/api/admin/security-log/settings", {"retention_days": 30}),
            ("PUT", "/api/admin/access-grants", {"subject_type": "user", "subject": "other", "grants": []}),
            ("DELETE", "/api/admin/access-grants/1", None),
        ]
        from fastapi.testclient import TestClient

        automation = TestClient(self.client.app)  # No cookies: the token alone
        for method, path, body in calls:
            refused = automation.request(method, path, json=body, headers=token)
            self.assertEqual(refused.status_code, 403, f"{method} {path}: {refused.text}")
            self.assertIn("signed in with a browser", refused.json()["detail"])
        self.assertEqual(automation.get("/api/admin/security", headers=token).status_code, 200)  # Reading the policy is still an admin token's
        with connect() as db:
            self.assertIsNone(db.execute("SELECT disabled_at FROM users WHERE id=?", (other,)).fetchone()[0])
        # A request that carries both is the token, as everywhere: the cookie does not rescue it
        both = self.client.put("/api/admin/security", json={"mfa_policy": "off"}, headers=token)
        self.assertEqual(both.status_code, 403)
        for method, path, body in calls:
            allowed = self.client.request(method, path, json=body)
            self.assertNotIn(allowed.status_code, (401, 403), f"{method} {path}: {allowed.text}")
        with connect() as db:
            self.assertIsNotNone(db.execute("SELECT disabled_at FROM users WHERE id=?", (other,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
