"""One destination rule for every camera address (#474), checked on the address actually connected to.

Refused always: link-local (cloud metadata included), unspecified, multicast and reserved addresses, and the NVR's own addresses. Loopback:
refused unless the server's configuration sets ``NVR_CAMERA_ALLOW_LOOPBACK`` (the tests set it, see ``tests/__init__.py``; the tests here
turn it off where they need to). Private addresses: allowed. Public addresses: refused unless an administrator signed in with a browser
ticks "This camera is at a public address" for that camera; Bulk Add and CSV import never do. Cameras that existed at the upgrade keep
working (``grandfathered``), are listed on the hardening page, and need the tick at their next source change.

No real public address appears here: ``203.0.113.0/24`` (a documentation range) stands in for the internet, through ``camera_destination.is_global``.
"""
from __future__ import annotations

import asyncio
import http.server
import ipaddress
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest.mock import AsyncMock, patch

import httpcore
from fastapi.testclient import TestClient

from app import camera_destination, hardening, ptz, recording
from app.application import create_app
from app.credentials import encrypt
from app.migrations import m0108_camera_public_address
from app.onvif_stream import CameraStreamSource
from app.recording import RecordingManager
from app.store import connect
from tests.test_support import TEST_CAMERA_PASSWORD, TEST_PASSWORD, patch_camera_probe, setup_code

_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH", "NVR_COOKIE_SECURE")
INTERNET = ipaddress.ip_network("203.0.113.0/24")  # Stands for the internet in these tests
PROBE = {"reachable": True, "video": {"codec": "h264", "width": 640, "height": 360}}
NAME = "camera.example.test"


def loopback_off():
    return patch.dict(os.environ, {"NVR_CAMERA_ALLOW_LOOPBACK": ""})


def internet():
    """203.0.113.0/24 is public for the length of the block."""
    return patch.object(camera_destination, "is_global", lambda address: address in INTERNET or address.is_global)


@contextmanager
def names(table: dict[str, list[str]]) -> Iterator[list[str]]:
    """``socket.getaddrinfo`` answers these names with these addresses; every name asked is listed in what is yielded."""
    real = socket.getaddrinfo
    asked: list[str] = []

    def lookup(host, port, *args, **kwargs):
        asked.append(host)
        if host in table:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port or 0, 0, 0)) if ":" in address
                    else (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0)) for address in table[host]]
        return real(host, port, *args, **kwargs)

    with patch("socket.getaddrinfo", lookup):
        yield asked


@contextmanager
def no_connections() -> Iterator[dict[str, AsyncMock]]:
    """Fails nothing by itself: records every TCP connection an HTTP client would make and every FFmpeg or ffprobe started."""
    connect_tcp = AsyncMock(side_effect=OSError("no network in this test"))
    spawn = AsyncMock(side_effect=OSError("no FFmpeg in this test"))
    with patch.object(httpcore.AnyIOBackend, "connect_tcp", connect_tcp), patch("asyncio.create_subprocess_exec", spawn):
        yield {"connect_tcp": connect_tcp, "spawn": spawn}


@contextmanager
def listener() -> Iterator[tuple[int, socket.socket]]:
    """A port on 127.0.0.1 that records whether anything connected to it."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.setblocking(False)
    try:
        yield server.getsockname()[1], server
    finally:
        server.close()


def connected(server: socket.socket) -> bool:
    try:
        client, _ = server.accept()
    except BlockingIOError:
        return False
    client.close()
    return True


class RuleTests(unittest.TestCase):
    def test_each_kind_of_address(self):
        cases = {
            "127.0.0.1": "loopback", "127.8.9.10": "loopback", "::1": "loopback", "::ffff:127.0.0.1": "loopback",
            "169.254.169.254": "link_local", "fe80::1": "link_local", "0.0.0.0": "unspecified", "0.1.2.3": "unspecified", "::": "unspecified",
            "224.0.0.1": "multicast", "ff02::1": "multicast", "240.0.0.1": "reserved", "255.255.255.255": "reserved",
            "10.1.2.3": "private", "172.20.0.1": "private", "192.168.1.5": "private", "100.64.1.1": "private", "fd00::5": "private",
        }
        with patch.object(camera_destination, "is_own", return_value=False):
            for address, kind in cases.items():
                with self.subTest(address=address):
                    self.assertEqual(camera_destination.classify(address), kind)
            self.assertEqual(camera_destination.classify("203.0.113.7"), "private")  # A documentation range: the internet does not route it
            with internet():
                self.assertEqual(camera_destination.classify("203.0.113.7"), "public")

    def test_refused_always_whatever_is_allowed(self):
        with patch.object(camera_destination, "is_own", return_value=False):
            for address in ("169.254.169.254", "fe80::1", "0.0.0.0", "224.0.0.1", "240.0.0.1"):
                with self.subTest(address=address), self.assertRaises(camera_destination.RefusedDestination):
                    camera_destination.check(address, allow_public=True)

    def test_the_nvrs_own_addresses_are_refused_even_with_loopback_allowed(self):
        self.assertTrue(camera_destination.is_own("127.0.0.1"))  # Only an address of this machine can be bound to
        with patch.object(camera_destination, "is_own", return_value=True), patch.dict(os.environ, {"NVR_CAMERA_ALLOW_LOOPBACK": "true"}):
            with self.assertRaises(camera_destination.RefusedDestination) as refused:
                camera_destination.check("10.20.30.40", allow_public=True)
        self.assertEqual(refused.exception.kind, "own")

    def test_loopback_follows_the_servers_configuration_only(self):
        with loopback_off():
            self.assertFalse(camera_destination.loopback_allowed())
            with self.assertRaises(camera_destination.RefusedDestination) as refused:
                camera_destination.check("127.0.0.1", allow_public=True)
            self.assertIn("NVR_CAMERA_ALLOW_LOOPBACK", str(refused.exception))
        with patch.dict(os.environ, {"NVR_CAMERA_ALLOW_LOOPBACK": "true"}):
            self.assertEqual(camera_destination.check("127.0.0.1"), "loopback")

    def test_a_public_address_needs_the_cameras_permission(self):
        with internet():
            with self.assertRaises(camera_destination.RefusedDestination) as refused:
                camera_destination.check("203.0.113.7")
            self.assertEqual(refused.exception.kind, "public")
            self.assertEqual(camera_destination.check("203.0.113.7", allow_public=True), "public")
            with camera_destination.public_permitted(True):
                self.assertEqual(camera_destination.check("203.0.113.7"), "public")
            with self.assertRaises(camera_destination.RefusedDestination):
                camera_destination.check("203.0.113.7")  # Only for the work it was given to

    def test_messages_hold_no_address(self):
        for kind, message in camera_destination.MESSAGES.items():
            self.assertNotRegex(message, r"\d+\.\d+\.\d+\.\d+/\d|example", kind)

    def test_every_address_a_name_gives_must_pass(self):
        with names({NAME: ["10.0.0.5", "169.254.169.254"]}):
            with self.assertRaises(camera_destination.RefusedDestination):
                camera_destination.resolve(NAME, 80)
            with self.assertRaises(camera_destination.RefusedDestination):
                camera_destination.pin_url(f"rtsp://{NAME}/stream")
        with names({NAME: ["fd00::5", "10.0.0.5"]}):
            self.assertEqual(camera_destination.resolve(NAME, 80), "10.0.0.5")  # IPv4 first

    def test_ffmpeg_is_given_the_checked_address_with_everything_else_as_written(self):
        with names({NAME: ["10.0.0.5"]}):
            self.assertEqual(camera_destination.pin_url(f"rtsp://viewer:p%40ss@{NAME}:8554/main?channel=1"), "rtsp://viewer:p%40ss@10.0.0.5:8554/main?channel=1")
        with names({NAME: ["fd00::5"]}):
            self.assertEqual(camera_destination.pin_url(f"rtsps://{NAME}/main"), "rtsps://[fd00::5]/main")
        self.assertEqual(camera_destination.pin_url("rtsp://[fd00::7]:554/x"), "rtsp://[fd00::7]:554/x")

    def test_a_refused_stream_is_never_opened(self):
        with names({NAME: ["169.254.169.254"]}), no_connections() as spies:
            result = asyncio.run(recording.probe_stream(f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@{NAME}/main"))
        self.assertFalse(result["reachable"])
        self.assertIn("link-local", result["reason"])
        self.assertNotIn(TEST_CAMERA_PASSWORD, json.dumps(result))
        spies["spawn"].assert_not_called()


class TransportTests(unittest.TestCase):
    """The HTTP client for a camera looks the name up once, checks it, and connects to that address with the name kept for the Host header."""

    def test_the_connection_goes_to_the_checked_address(self):
        seen: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - the standard library's name
                seen.append(self.headers["Host"])
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        async def fetch() -> int:
            async with camera_destination.camera_client() as client:
                return (await client.get(f"http://{NAME}:{port}/onvif/device_service")).status_code

        try:
            with names({NAME: ["127.0.0.1"]}) as asked:
                self.assertEqual(asyncio.run(fetch()), 200)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(seen, [f"{NAME}:{port}"])
        self.assertEqual(asked.count(NAME), 1)  # Looked up once: the connection is made to the address that was checked

    def test_a_name_that_answers_a_private_address_and_another_is_refused_before_connecting(self):
        async def fetch() -> None:
            async with camera_destination.camera_client() as client:
                await client.get(f"http://{NAME}/onvif/device_service")

        for answer in (["10.0.0.5", "169.254.169.254"], ["10.0.0.5", "127.0.0.1"]):
            with self.subTest(answer=answer), names({NAME: answer}), loopback_off(), no_connections() as spies:
                with self.assertRaises(camera_destination.RefusedDestination):
                    asyncio.run(fetch())
                spies["connect_tcp"].assert_not_called()

    def test_a_proxy_in_the_environment_is_not_used_for_cameras(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://198.51.100.9:3128", "HTTPS_PROXY": "http://198.51.100.9:3128"}):
            client = camera_destination.camera_client()
        self.assertFalse(client._trust_env)  # noqa: SLF001
        asyncio.run(client.aclose())


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous = {key: os.environ.get(key) for key in _ENV}
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        os.environ["NVR_COOKIE_SECURE"] = "false"
        self.app = create_app(start_workers=False)
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()
        self.client.post("/api/setup/admin", json={"username": "destination-admin", "password": TEST_PASSWORD, "setup_code": setup_code()})
        self.app.state.recorders = SimpleNamespace(sync_camera=AsyncMock())  # No recorder here: what it is told is not what these tests are about

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def token(self) -> str:
        made = self.client.post("/api/admin/api-tokens", json={"name": "Automation", "scopes": ["read", "admin"]})
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()["secret"]

    def audits(self, action: str) -> list[dict]:
        with connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT details_json FROM audit_log WHERE action=? ORDER BY id", (action,))]

    def camera(self, stream: str = f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@192.168.1.20:554/main", **fields) -> str:
        with patch_camera_probe(new=AsyncMock(return_value=PROBE)):
            created = self.client.post("/api/cameras", json={"name": fields.pop("name", "Gate"), "stream_url": stream, "recording_mode": "continuous", **fields})
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()["id"]

    def row(self, camera_id: str) -> sqlite3.Row:
        with connect() as db:
            return db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()

    # --- inspection, the stream test, Bulk Add and CSV import -------------------------------------------------------------------------

    def test_inspecting_loopback_is_refused_before_any_connection(self):
        with listener() as (port, server), loopback_off():
            for path in ("/api/cameras/inspect", "/api/cameras/inspect/stream"):
                answer = self.client.post(path, json={"host": "127.0.0.1", "device_service_url": f"http://127.0.0.1:{port}/onvif/device_service", "verify_video": False})
                if path.endswith("stream"):
                    self.assertEqual(json.loads(answer.text.splitlines()[-1])["type"], "error")
                    self.assertIn("loopback", answer.text)
                else:
                    self.assertEqual(answer.status_code, 422, answer.text)
                    self.assertIn("loopback", answer.json()["detail"])
            self.assertFalse(connected(server))

    def test_a_name_that_resolves_to_the_metadata_address_is_refused_before_any_connection(self):
        with names({NAME: ["169.254.169.254"]}), no_connections() as spies:
            inspected = self.client.post("/api/cameras/inspect", json={"host": NAME, "verify_video": False})
            tested = self.client.post("/api/cameras/test", json={"stream_url": f"rtsp://{NAME}:554/main"})
            profiles = self.client.post("/api/cameras/onvif-profiles", json={"camera_ip": NAME})
        for answer in (inspected, tested, profiles):
            self.assertEqual(answer.status_code, 422, answer.text)
            self.assertIn("link-local", answer.json()["detail"])
            self.assertNotIn(NAME, answer.json()["detail"])
        spies["connect_tcp"].assert_not_called()
        spies["spawn"].assert_not_called()

    def test_the_stream_test_refuses_a_name_with_one_bad_answer_among_good_ones(self):
        with names({NAME: ["192.168.1.20", "169.254.169.254"]}), no_connections() as spies:
            answer = self.client.post("/api/cameras/test", json={"stream_url": f"rtsp://{NAME}:554/main"})
        self.assertEqual(answer.status_code, 422, answer.text)
        spies["spawn"].assert_not_called()

    def test_bulk_add_rows_are_held_to_the_rule_and_never_allowed_a_public_address(self):
        made = self.client.post("/api/cameras/bulk-jobs", json={"cameras": [{"host": "127.0.0.1"}, {"host": "203.0.113.9"}, {"host": NAME}]})
        self.assertEqual(made.status_code, 201, made.text)
        with loopback_off(), internet(), names({NAME: ["169.254.169.254"]}), no_connections() as spies, \
                patch("app.api.cameras.add_camera", new=AsyncMock()) as add:
            asyncio.run(self.app.state.bulk_add.run_until_idle())
        items = self.client.get(f"/api/cameras/bulk-jobs/{made.json()['id']}").json()["items"]
        self.assertEqual([item["status"] for item in items], ["failed", "failed", "failed"])
        self.assertIn("loopback", items[0]["detail"])
        self.assertIn("public internet address", items[1]["detail"])
        self.assertIn("link-local", items[2]["detail"])
        spies["connect_tcp"].assert_not_called()
        add.assert_not_called()

    def test_bulk_add_saves_through_the_same_rule(self):
        # Even were an inspection to pass, the add itself is held to the rule with no permission for a public address
        with internet(), no_connections():
            with self.assertRaises(Exception) as refused:
                from app.api.cameras import CameraCreateRequest, add_camera
                asyncio.run(add_camera(CameraCreateRequest(name="Far", camera_ip="203.0.113.9"), None))
        self.assertEqual(getattr(refused.exception, "status_code", None), 422)

    def test_csv_import_rows_are_held_to_the_rule(self):
        csv_text = "name,stream_url\nLoop,rtsp://127.0.0.1:8554/main\nFar,rtsp://203.0.113.9:554/main\nMeta,rtsp://169.254.169.254/main\n"
        with loopback_off(), internet(), no_connections() as spies:
            answer = self.client.post("/api/cameras/import/apply", json={"csv_text": csv_text})
        self.assertEqual(answer.status_code, 200, answer.text)
        rows = answer.json()["rows"]
        self.assertEqual([row["status"] for row in rows], ["failed", "failed", "failed"])
        self.assertIn("loopback", rows[0]["errors"][0])
        self.assertIn("public internet address", rows[1]["errors"][0])
        self.assertIn("link-local", rows[2]["errors"][0])
        spies["spawn"].assert_not_called()

    # --- stored device addresses -------------------------------------------------------------------------------------------------------

    def test_a_stored_device_address_is_held_to_the_rule_when_it_is_used(self):
        with listener() as (port, server):
            camera_id = self.camera("rtsp://127.0.0.1:8554/main", device_service_url=f"http://127.0.0.1:{port}/onvif/device_service")
            ptz.forget_capabilities()
            with loopback_off():
                found, reason = asyncio.run(ptz.capabilities(camera_id))
                self.assertIsNone(found)
                self.assertFalse(connected(server))
                from app import onvif_masks
                with self.assertRaises(camera_destination.RefusedDestination):
                    asyncio.run(onvif_masks.read_masks(f"http://127.0.0.1:{port}/onvif/device_service", "", ""))
                self.assertFalse(connected(server))
                # The recorder: the camera's status says why, with no address in it
                self.assertIsNone(RecordingManager()._continuous_source(camera_id, self.row(camera_id)))  # noqa: SLF001
                self.assertIn("loopback", self.row(camera_id)["last_error"])

    def test_a_stored_name_is_looked_up_once_and_ffmpeg_given_the_address(self):
        camera_id = self.camera()
        with connect() as db:
            db.execute("UPDATE cameras SET stream_url_ciphertext=? WHERE id=?", (encrypt(f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@{NAME}:554/main"), camera_id))
        with names({NAME: ["192.168.1.21"]}):
            source = RecordingManager()._continuous_source(camera_id, self.row(camera_id))  # noqa: SLF001
        self.assertEqual(source, f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@192.168.1.21:554/main")
        with names({NAME: ["192.168.1.21", "169.254.169.254"]}):
            self.assertIsNone(RecordingManager()._continuous_source(camera_id, self.row(camera_id)))  # noqa: SLF001

    # --- public addresses ----------------------------------------------------------------------------------------------------------------

    def test_a_public_address_needs_the_tick_from_a_browser_session_and_is_audited(self):
        stream = f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@203.0.113.7:554/main"
        with internet(), patch_camera_probe(new=AsyncMock(return_value=PROBE)):
            refused = self.client.post("/api/cameras", json={"name": "Far", "stream_url": stream})
            self.assertEqual(refused.status_code, 422, refused.text)
            self.assertIn("This camera is at a public address", refused.json()["detail"])
            tested = self.client.post("/api/cameras/test", json={"stream_url": stream})
            self.assertEqual(tested.status_code, 422, tested.text)

            secret = self.token()
            with TestClient(self.app) as bearer:  # No session cookie: only the token signs this in
                ticked = bearer.post("/api/cameras", json={"name": "Far", "stream_url": stream, "public_address": True}, headers={"Authorization": f"Bearer {secret}"})
                plain = bearer.post("/api/cameras", json={"name": "Far", "stream_url": stream}, headers={"Authorization": f"Bearer {secret}"})
                inspected = bearer.post("/api/cameras/inspect", json={"host": "203.0.113.7", "public_address": True}, headers={"Authorization": f"Bearer {secret}"})
            self.assertEqual(ticked.status_code, 403, ticked.text)
            self.assertEqual(plain.status_code, 422, plain.text)
            self.assertEqual(inspected.status_code, 403, inspected.text)
            self.assertEqual(self.audits("camera_public_address_allowed"), [])

            created = self.client.post("/api/cameras", json={"name": "Far", "stream_url": stream, "public_address": True})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["public_address"], "allowed")
        self.assertEqual(self.row(created.json()["id"])["public_address"], "allowed")
        self.assertEqual(self.audits("camera_public_address_allowed"), [{"change": "added"}])
        self.assertNotIn(TEST_CAMERA_PASSWORD, created.text)

    def test_the_tick_on_a_private_address_allows_nothing(self):
        camera_id = self.camera(public_address=True)
        self.assertIsNone(self.row(camera_id)["public_address"])
        self.assertEqual(self.audits("camera_public_address_allowed"), [])

    def test_an_allowed_public_camera_is_reached_and_one_without_permission_is_not(self):
        with internet():
            with patch_camera_probe(new=AsyncMock(return_value=PROBE)):
                camera_id = self.camera(f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@203.0.113.7:554/main", public_address=True)
            self.assertEqual(RecordingManager()._continuous_source(camera_id, self.row(camera_id)), f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@203.0.113.7:554/main")  # noqa: SLF001
            with connect() as db:
                db.execute("UPDATE cameras SET public_address=NULL WHERE id=?", (camera_id,))
            self.assertIsNone(RecordingManager()._continuous_source(camera_id, self.row(camera_id)))  # noqa: SLF001
            self.assertIn("public internet address", self.row(camera_id)["last_error"])

    # --- cameras from before the rule ------------------------------------------------------------------------------------------------------

    def test_an_existing_public_camera_keeps_working_is_listed_and_needs_the_tick_at_its_next_source_change(self):
        camera_id = self.camera()
        old_stream = f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@203.0.113.8:554/main"
        with connect() as db:  # As the upgrade leaves a camera that was already at a public address
            db.execute("UPDATE cameras SET stream_url_ciphertext=?,public_address='grandfathered' WHERE id=?", (encrypt(old_stream), camera_id))
        with internet():
            self.assertEqual(RecordingManager()._continuous_source(camera_id, self.row(camera_id)), old_stream)  # noqa: SLF001
            with connect() as db:
                listed = hardening.report(db)["public_cameras"]
            self.assertEqual(listed, [{"camera_id": camera_id, "camera_name": "Gate", "permission": "grandfathered"}])
            self.assertEqual(self.client.get("/api/hardening").json()["public_cameras"], listed)

            new_stream = f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@203.0.113.8:8554/main"
            with patch_camera_probe(new=AsyncMock(return_value=PROBE)):
                refused = self.client.put(f"/api/cameras/{camera_id}/source", json={"stream_url": new_stream})
                self.assertEqual(refused.status_code, 422, refused.text)
                self.assertEqual(self.row(camera_id)["public_address"], "grandfathered")
                with patch("app.api.cameras.resolve_camera_stream", new=AsyncMock(return_value=CameraStreamSource(new_stream, None))):
                    by_address = self.client.put(f"/api/cameras/{camera_id}/source", json={"camera_ip": "203.0.113.8"})
                self.assertEqual(by_address.status_code, 422, by_address.text)
                saved = self.client.put(f"/api/cameras/{camera_id}/source", json={"stream_url": new_stream, "public_address": True})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(self.row(camera_id)["public_address"], "allowed")
            self.assertEqual(self.audits("camera_public_address_allowed"), [{"change": "source", "before": "grandfathered"}])
            with connect() as db:
                self.assertEqual(hardening.report(db)["public_cameras"][0]["permission"], "allowed")

    def test_a_camera_moved_to_a_private_address_loses_its_permission(self):
        camera_id = self.camera()
        with connect() as db:
            db.execute("UPDATE cameras SET public_address='grandfathered' WHERE id=?", (camera_id,))
        with patch_camera_probe(new=AsyncMock(return_value=PROBE)):
            saved = self.client.put(f"/api/cameras/{camera_id}/source", json={"stream_url": f"rtsp://viewer:{TEST_CAMERA_PASSWORD}@192.168.1.22:554/main"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertIsNone(self.row(camera_id)["public_address"])
        with connect() as db:
            self.assertEqual(hardening.report(db)["public_cameras"], [])  # A private camera from before the rule is not listed

    def test_the_hardening_page_warns_while_loopback_is_allowed(self):
        self.assertTrue(self.client.get("/api/hardening").json()["camera_loopback_allowed"])
        with loopback_off():
            self.assertFalse(self.client.get("/api/hardening").json()["camera_loopback_allowed"])


class MigrationTests(unittest.TestCase):
    def test_every_camera_at_the_upgrade_keeps_working_and_new_ones_need_permission(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE cameras(id TEXT PRIMARY KEY, name TEXT)")
        db.executemany("INSERT INTO cameras(id,name) VALUES(?,?)", [("a", "Gate"), ("b", "Yard")])
        m0108_camera_public_address.upgrade(db)
        m0108_camera_public_address.upgrade(db)  # Twice is the same as once
        db.execute("INSERT INTO cameras(id,name) VALUES('c','New')")
        self.assertEqual(db.execute("SELECT id,public_address FROM cameras ORDER BY id").fetchall(), [("a", "grandfathered"), ("b", "grandfathered"), ("c", None)])
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE cameras SET public_address='anything' WHERE id='c'")


if __name__ == "__main__":
    unittest.main()
