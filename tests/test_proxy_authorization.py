"""A signed-in browser still works behind a proxy that adds its own Authorization header (#465).

Any Authorization header was taken for an API token attempt, and a scheme other than Bearer refused, so a browser behind a reverse proxy with
HTTP Basic sign-in had every request answered 401. Only a Bearer header is a token now, and it still wins over a session cookie (#292)."""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.principal import COOKIE_NAME, is_bearer
from tests.test_api_tokens import APITokenTests

BASIC = {"Authorization": "Basic dXNlcjpwYXNz"}


class ProxyAuthorizationTests(unittest.TestCase):
    setUp = APITokenTests.setUp  # A fresh application with an administrator signed in; not its tests
    tearDown = APITokenTests.tearDown
    _create = APITokenTests._create

    def browser(self, cookie: str | None) -> TestClient:
        client = TestClient(self.client.app)
        self.addCleanup(client.close)
        if cookie:
            client.cookies.set(COOKIE_NAME, cookie)
        return client

    def test_a_session_with_a_basic_header_is_the_session(self):
        browser = self.browser(self.admin_cookie)
        for header in (BASIC, {"Authorization": "basic dXNlcjpwYXNz"}, {"Authorization": "Negotiate YIIB"}, {"Authorization": "Digest username=\"user\""}):
            with self.subTest(header=header["Authorization"].split()[0]):
                me = browser.get("/api/auth/me", headers=header)
                self.assertEqual(me.status_code, 200, me.text)
                self.assertEqual(me.json()["username"], "keeper")
                self.assertEqual(browser.get("/api/cameras", headers=header).status_code, 200)
        # A change is accepted too, as the session's
        self.assertEqual(browser.post("/api/admin/api-tokens", headers=BASIC, json={"name": "Through the proxy", "scopes": ["read"]}).status_code, 201)

    def test_without_a_session_a_basic_header_is_still_refused(self):
        self.assertEqual(self.browser(None).get("/api/auth/me", headers=BASIC).status_code, 401)

    def test_a_bearer_header_still_wins_over_a_session_cookie(self):
        _, secret = self._create("read")
        browser = self.browser(self.admin_cookie)
        me = browser.get("/api/admin/api-tokens", headers={"Authorization": f"Bearer {secret}"})
        self.assertEqual(me.status_code, 403, me.text)  # The read token, not the administrator's session
        for presented in ("Bearer ", "Bearer", "bearer nvrt_unknown_secret", "Bearer garbage"):
            with self.subTest(presented=presented):
                refused = browser.get("/api/auth/me", headers={"Authorization": presented})
                self.assertEqual(refused.status_code, 401)
                self.assertEqual(refused.headers["www-authenticate"], "Bearer")

    def test_the_live_socket_handshake_follows_the_same_rule(self):
        path = "/api/cameras/00000000-0000-0000-0000-000000000000/live/stream"

        def first(client: TestClient, headers: dict) -> str:
            with client.websocket_connect(path, headers=headers) as socket:
                message = socket.receive_json()
                with self.assertRaises(WebSocketDisconnect):
                    socket.receive_json()
            return message["type"]
        # Past sign-in, to the camera (which does not exist), with the proxy's header beside the session
        self.assertEqual(first(self.browser(self.admin_cookie), BASIC), "not_found")
        self.assertEqual(first(self.browser(None), BASIC), "unauthorized")
        self.assertEqual(first(self.browser(self.admin_cookie), {"Authorization": "Bearer garbage"}), "unauthorized")

    def test_which_headers_are_bearer(self):
        for header, bearer in (("Bearer x", True), ("bearer x", True), ("BEARER", True), (" Bearer x", True), ("Basic x", False),
                               ("Bearerx y", False), ("", False), ("Token x", False)):
            with self.subTest(header=header):
                self.assertIs(is_bearer(header), bearer)


if __name__ == "__main__":
    unittest.main()
