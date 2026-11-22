"""Error text shown to people who are not administrators does not hold a camera's address (#471, after #377).

Documentation addresses only (192.0.2.0/24, 2001:db8::/32, example.test)."""
from __future__ import annotations

import json
import unittest
import uuid

import httpx
from fastapi.testclient import TestClient

from app.migrations import m0090_no_address_in_onvif_errors as step
from app.onvif_client import OnvifClient, OnvifError
from app.security import hash_password
from app.store import connect
from tests import test_bootstrap
from tests.test_support import TEST_PASSWORD, setup_code

HOSTS = ("192.0.2.10", "2001:db8::10", "camera-7.example.test")
OPERATOR_PASSWORD = "operator-password-1234"


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


async def message_for(host: str, device_service_url: str | None) -> str:
    client = OnvifClient(host, transport=httpx.MockTransport(unreachable))
    try:
        await client.locate_device(device_service_url)
    except OnvifError as exc:
        return str(exc)
    finally:
        await client._http.aclose()
    raise AssertionError("an ONVIF service that does not answer was located")


class MessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_message_for_a_service_that_does_not_answer_names_no_host_and_still_says_what_to_check(self):
        for host in HOSTS:
            bracketed = f"[{host}]" if ":" in host else host
            for given in (None, f"http://{bracketed}:8899/onvif/device_service"):
                said = await message_for(host, given)
                self.assertNotIn(host, said)
                self.assertTrue(said.startswith("No ONVIF service answered"), said)
                self.assertIn("check the address, that ONVIF is enabled on the camera, and the ONVIF port", said)  # Something an administrator can act on
        self.assertIn("on port 80, ", await message_for(HOSTS[0], None))  # The ports tried are still said: they are not the camera's address


class StoredTextTests(unittest.TestCase):
    setUp = test_bootstrap.BootstrapTests.setUp
    tearDown = test_bootstrap.BootstrapTests.tearDown

    OLD = ("Motion events: No ONVIF service answered at the device service address for {host}: check the address, that ONVIF is enabled on the camera, and the ONVIF port",
           "No ONVIF service answered at {host} on port 80, 8000, 8080, 8899: check the address, that ONVIF is enabled on the camera, and the ONVIF port")

    def test_the_two_old_forms_lose_their_host_and_nothing_else(self):
        for host in HOSTS:
            self.assertEqual(step.without_host(self.OLD[0].format(host=host)),
                             "Motion events: No ONVIF service answered at the device service address: check the address, that ONVIF is enabled on the camera, and the ONVIF port")
            self.assertEqual(step.without_host(self.OLD[1].format(host=host)),
                             "No ONVIF service answered on port 80, 8000, 8080, 8899: check the address, that ONVIF is enabled on the camera, and the ONVIF port")
        self.assertEqual(step.without_host("Camera authentication rejected"), "Camera authentication rejected")

    def test_text_stored_before_is_cleaned_and_an_operator_and_a_read_token_see_no_address_while_an_administrator_can_still_act(self):
        created = self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD, "setup_code": setup_code()})
        self.assertEqual(created.status_code, 201, created.text)
        host, camera_id = HOSTS[0], str(uuid.uuid4())
        old, older = self.OLD[0].format(host=host), self.OLD[1].format(host=host)
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('viewer',?,'operator')", (hash_password(OPERATOR_PASSWORD),))
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,last_error,backfill_support_message) VALUES(?,?,?,?,?)", (camera_id, "Gate", b"encrypted", old, older))
            db.execute("INSERT INTO camera_capabilities(camera_id,capability,state,message,checked_at) VALUES(?,'motion','failed',?,'2026-11-20T10:00:00.000Z')", (camera_id, old))
            db.execute("INSERT INTO incidents(id,incident_key,camera_id,fault_type,summary,state,fault_active,recurrence_count,first_seen_at,last_seen_at,details_json) "
                       "VALUES(?,?,?,'onvif',?,'active',1,1,'2026-11-20T10:00:00.000Z','2026-11-20T10:00:00.000Z',?)",
                       (str(uuid.uuid4()), f"onvif:{camera_id}", camera_id, older, json.dumps({"error": old})))
            step.upgrade(db)
            step.upgrade(db)  # Run twice: nothing more to change
            kept = [db.execute("SELECT last_error,backfill_support_message FROM cameras WHERE id=?", (camera_id,)).fetchone(),
                    db.execute("SELECT message FROM camera_capabilities WHERE camera_id=?", (camera_id,)).fetchone(),
                    db.execute("SELECT summary,details_json FROM incidents WHERE camera_id=?", (camera_id,)).fetchone()]
        for row in kept:
            for text in tuple(row):
                self.assertNotIn(host, text)
                self.assertIn("No ONVIF service answered", text)

        secret = self.client.post("/api/admin/api-tokens", json={"name": "Reader", "scopes": ["read"]}).json()["secret"]
        operator = TestClient(self.client.app)
        self.assertEqual(operator.post("/api/auth/login", json={"username": "viewer", "password": OPERATOR_PASSWORD}).status_code, 200)
        reader = TestClient(self.client.app)
        paths = ("/api/cameras", f"/api/cameras/{camera_id}/capabilities", f"/api/cameras/{camera_id}/backfill", "/api/incidents")
        for name, caller, headers in (("operator", operator, {}), ("read token", reader, {"Authorization": f"Bearer {secret}"})):
            for path in paths:
                answer = caller.get(path, headers=headers)
                self.assertEqual(answer.status_code, 200, f"{name} {path}: {answer.text[:200]}")
                self.assertNotIn(host, answer.text, f"{name} {path}")
        listed = self.client.get("/api/cameras").json()["cameras"][0]["last_error"]
        self.assertIn("check the address, that ONVIF is enabled on the camera, and the ONVIF port", listed)  # The administrator can still act on it


if __name__ == "__main__":
    unittest.main()
