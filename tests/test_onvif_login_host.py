"""A camera's saved ONVIF login goes only to the hosts it has gone to before (#472).

The saved login is sent to the camera's stored device and event addresses (PTZ, masks, health, backfill, motion events). Moving either
address to another host, without entering the login again, would hand whoever runs that host a digest of the password to guess offline.
Documentation addresses only."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from app.credentials import decrypt
from app.onvif_stream import CameraStreamSource
from app.store import connect
import unittest

from tests import test_camera_inventory
from tests.test_support import TEST_CAMERA_PASSWORD, patch_camera_probe

PROBE = {"reachable": True, "video": {"codec": "h264", "width": 640, "height": 360}}
LOGIN_PASSWORD = "onvif-login-password-7731"


class OnvifLoginHostTests(unittest.TestCase):
    setUp = test_camera_inventory.CameraInventoryTests.setUp  # A signed-in administrator; the inventory tests themselves are not run again
    tearDown = test_camera_inventory.CameraInventoryTests.tearDown

    def camera(self) -> str:
        with patch_camera_probe(new=AsyncMock(return_value=PROBE)):
            created = self.client.post("/api/cameras", json={
                "name": "Gate", "stream_url": f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@192.0.2.10:554/main", "recording_mode": "disabled",
                "device_service_url": "http://192.0.2.10/onvif/device_service", "motion_service_url": "http://192.0.2.10/onvif/device_service",
                "motion_username": "onvif-user", "motion_password": LOGIN_PASSWORD})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()["id"]

    def login(self, camera_id: str) -> dict | None:
        with connect() as db:
            row = db.execute("SELECT motion_credentials_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
        return json.loads(decrypt(row[0])) if row[0] else None

    def audits(self, action: str) -> list[dict]:
        with connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT details_json FROM audit_log WHERE action=? ORDER BY id", (action,))]

    def motion(self, camera_id: str, **body):
        with patch.object(self.client.app.state.recorders, "sync_camera", new=AsyncMock()):
            return self.client.post(f"/api/cameras/{camera_id}/motion-source", json=body)

    def source(self, camera_id: str, body: dict, resolved: str | None = None):
        stream = f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@{body.get('camera_ip', '192.0.2.10')}:554/main"
        with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(return_value=CameraStreamSource(stream, resolved))), \
                patch_camera_probe(new=AsyncMock(return_value=PROBE)), patch.object(self.client.app.state.recorders, "sync_camera", new=AsyncMock()):
            return self.client.put(f"/api/cameras/{camera_id}/source", json=body)

    def test_moving_the_event_address_to_another_host_needs_the_login_again(self):
        camera_id = self.camera()
        refused = self.motion(camera_id, service_url="http://203.0.113.50/onvif/device_service")
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertIn("Enter the ONVIF username and password again", refused.json()["detail"])
        with connect() as db:
            self.assertEqual(db.execute("SELECT motion_service_url FROM cameras WHERE id=?", (camera_id,)).fetchone()[0], "http://192.0.2.10/onvif/device_service")
        self.assertEqual(self.audits("camera_motion_source_changed"), [])  # Nothing changed, nothing recorded

        # With the login entered again, it goes there, and the change is recorded without any login in it
        moved = self.motion(camera_id, service_url="http://203.0.113.50/onvif/device_service", username="other-user", password="other-password-55")
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(self.login(camera_id), {"username": "other-user", "password": "other-password-55"})
        (entry,) = self.audits("camera_motion_source_changed")
        self.assertEqual((entry["event_host"], entry["onvif_login"]), ("203.0.113.50", "replaced"))
        self.assertNotIn("other-password-55", json.dumps(entry))

    def test_another_path_or_port_on_the_same_host_keeps_the_login(self):
        camera_id = self.camera()
        for address in ("http://192.0.2.10:8899/onvif/device_service", "https://192.0.2.10/onvif/events"):
            kept = self.motion(camera_id, service_url=address)
            self.assertEqual(kept.status_code, 200, kept.text)
            self.assertEqual(self.login(camera_id), {"username": "onvif-user", "password": LOGIN_PASSWORD})
        self.assertEqual([entry["onvif_login"] for entry in self.audits("camera_motion_source_changed")], ["kept", "kept"])

    def test_a_source_change_that_moves_the_device_address_to_another_host_clears_the_login_unless_one_is_entered(self):
        camera_id = self.camera()
        # A direct stream with a device address on another host: the saved login does not go with it
        moved = self.source(camera_id, {"stream_url": f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@192.0.2.10:554/main", "device_service_url": "http://203.0.113.50/onvif/device_service"})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertIsNone(self.login(camera_id))
        (entry,) = self.audits("camera_source_changed")
        self.assertEqual((entry["device_host"], entry["onvif_login"]), ("203.0.113.50", "cleared"))
        self.assertNotIn(TEST_CAMERA_PASSWORD, json.dumps(entry))

    def test_a_camera_given_a_new_address_with_its_login_takes_that_login(self):
        camera_id = self.camera()
        moved = self.source(camera_id, {"camera_ip": "192.0.2.77", "camera_username": "new-user", "camera_password": "new-password-123",
                                        "onvif_profile_token": "main"}, resolved="http://192.0.2.77/onvif/device_service")
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(self.login(camera_id), {"username": "new-user", "password": "new-password-123"})  # As adding a camera does
        self.assertEqual(self.audits("camera_source_changed")[-1]["onvif_login"], "replaced")

    def test_a_source_change_on_the_same_host_keeps_the_login(self):
        camera_id = self.camera()
        kept = self.source(camera_id, {"stream_url": f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@192.0.2.10:554/sub", "device_service_url": "http://192.0.2.10:8080/onvif/device_service"})
        self.assertEqual(kept.status_code, 200, kept.text)
        self.assertEqual(self.login(camera_id), {"username": "onvif-user", "password": LOGIN_PASSWORD})
        self.assertEqual(self.audits("camera_source_changed")[-1]["onvif_login"], "kept")


if __name__ == "__main__":
    unittest.main()
