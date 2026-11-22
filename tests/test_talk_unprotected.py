"""Two-way audio never hands a camera its password unprotected unless an administrator allowed it for that camera (#476).

A camera that asks for Basic sign-in over plain RTSP is asking for the user name and password merely encoded: anything answering at the
camera's address could ask, and read them. The stand-in camera here asks for Basic, for Digest, or offers both."""
from __future__ import annotations

import asyncio
import json
import unittest


from app import talk
from app.credentials import encrypt
from app.store import connect
from tests import test_talk
from tests.fixtures.rtsp_backchannel import BackchannelCamera

PASSWORD = "S3cret!pass"


class BackchannelSignInTests(unittest.TestCase):
    def camera(self, sign_in: str) -> BackchannelCamera:
        camera = BackchannelCamera(username="operator", password=PASSWORD, sign_in=sign_in)
        self.addCleanup(camera.close)
        return camera

    def sent(self, camera: BackchannelCamera) -> list[str]:
        return [headers.get("authorization", "") for _, headers in camera.requests]

    def connect(self, camera: BackchannelCamera, allow_basic: bool = False):
        async def run():
            channel = talk.RtspBackchannel(camera.url, allow_basic=allow_basic)
            try:
                return await channel.connect()
            finally:
                await channel.close()
        return asyncio.run(run())

    def test_a_camera_asking_only_for_basic_is_sent_no_password_and_the_reason_is_plain(self):
        camera = self.camera("basic")
        with self.assertRaises(talk.BackchannelError) as refused:
            self.connect(camera)
        self.assertIn("asked for its password to be sent unprotected", str(refused.exception))
        self.assertTrue(camera.requests)  # It was asked, and answered with nothing
        self.assertEqual([header for header in self.sent(camera) if header], [])

    def test_digest_still_works_and_is_chosen_when_both_are_offered(self):
        for sign_in in ("digest", "both"):
            camera = self.camera(sign_in)
            self.assertEqual(self.connect(camera).codec, "PCMU", sign_in)
            used = [header for header in self.sent(camera) if header]
            self.assertTrue(used and all(header.lower().startswith("digest ") for header in used), (sign_in, used))

    def test_a_camera_allowed_unprotected_sign_in_is_answered(self):
        camera = self.camera("basic")
        self.assertEqual(self.connect(camera, allow_basic=True).codec, "PCMU")
        self.assertTrue(any(header.lower().startswith("basic ") for header in self.sent(camera)))


class AllowanceTests(test_talk.TalkApiTests):
    """The allowance through the API, the security log and the hardening page. Only these tests run here, not the talk tests borrowed from."""

    def setUp(self):
        test_talk.SceneTestCase.setUp(self)
        self.camera = BackchannelCamera(username="operator", password=PASSWORD, sign_in="basic")
        self.addCleanup(self.camera.close)
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=? WHERE id=?", (encrypt(self.camera.url), self.camera_id))
        self.path = f"/api/cameras/{self.camera_id}/talk"
        self.stream = f"{self.path}/stream"

    def test_a_talk_to_a_camera_asking_for_basic_ends_with_the_reason_until_it_is_allowed(self):
        settings = self.enable()
        self.assertFalse(settings["allow_unprotected_sign_in"])
        message, code = self.refused(self.client)
        self.assertIn("asked for its password to be sent unprotected", message["detail"])
        self.assertNotIn(PASSWORD, json.dumps(message))
        self.assertFalse([headers for _, headers in self.camera.requests if headers.get("authorization")])

        # An administrator allows it for this one camera: it is in the security log and on the hardening page, and talking works
        self.assertTrue(self.enable(allow_unprotected_sign_in=True)["allow_unprotected_sign_in"])
        self.assertEqual(len(self.audit("camera_talk_unprotected_sign_in_allowed")), 1)
        listed = self.client.get("/api/hardening").json()["unprotected_talk"]
        self.assertEqual([entry["camera_id"] for entry in listed], [self.camera_id])
        with self.client.websocket_connect(self.stream) as websocket:
            self.assertEqual(websocket.receive_json()["type"], "ready")
            websocket.send_text(json.dumps({"type": "stop"}))
            self.assertEqual(websocket.receive_json()["type"], "ended")

        # Saving the other talk settings without naming it keeps it; turning it off is logged and leaves the page
        self.assertTrue(self.client.put(self.path, json={"enabled": True, "roles": ["administrator"], "record_audio": True}).json()["allow_unprotected_sign_in"])
        self.assertFalse(self.enable(allow_unprotected_sign_in=False)["allow_unprotected_sign_in"])
        self.assertEqual(len(self.audit("camera_talk_unprotected_sign_in_refused")), 1)
        self.assertEqual(self.client.get("/api/hardening").json()["unprotected_talk"], [])

    def test_only_administrators_see_or_set_it(self):
        self.enable(roles=["administrator", "operator"])
        other = self.operator()
        self.assertNotIn("allow_unprotected_sign_in", other.get(self.path).json())
        self.assertEqual(other.put(self.path, json={"enabled": True, "roles": ["operator"], "allow_unprotected_sign_in": True}).status_code, 403)


# Only the tests written here: the talk API tests borrowed from are run in their own module
for name in [name for name in dir(test_talk.TalkApiTests) if name.startswith("test_") and name not in AllowanceTests.__dict__]:
    setattr(AllowanceTests, name, None)

if __name__ == "__main__":
    unittest.main()
