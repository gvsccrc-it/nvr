"""The interface and JSON answers are sent compressed, and nothing that must not be is (#537)."""
from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Depends, FastAPI, WebSocket
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from app import compression
from app.api import web_interface
from app.compression import JsonCompression, no_compression

BIG = {"cameras": [{"id": f"camera-{index}", "name": f"Camera {index}", "recording": "recording"} for index in range(200)]}
SMALL = {"ok": True}
GZIP = {"Accept-Encoding": "gzip, deflate, br"}


def stand_in() -> FastAPI:
    """Every kind of answer the NVR sends, behind the middleware as the application has it."""
    app = FastAPI()

    @app.get("/json")
    def big_json():
        return BIG

    @app.get("/small")
    def small_json():
        return SMALL

    @app.post("/json")
    def created():  # A new API token, a signing secret, recovery codes: every such answer is to a POST
        return {"secret": "s" * 40, **BIG}

    @app.get("/secret", dependencies=[Depends(no_compression)])
    def secret_get():
        return {"secret": "s" * 40, **BIG}

    @app.get("/stream")
    def stream():
        def pieces():
            yield json.dumps(BIG).encode()[:2000]
            yield json.dumps(BIG).encode()[2000:]
        return StreamingResponse(pieces(), media_type="application/json")

    @app.get("/lines")
    def lines():
        return StreamingResponse((json.dumps(item).encode() + b"\n" for item in BIG["cameras"]), media_type="application/x-ndjson")

    @app.get("/video")
    def video():
        return Response(b"\x00" * 50_000, media_type="video/mp4")

    @app.get("/mjpeg")
    def mjpeg():
        return StreamingResponse((b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + b"\xff" * 5000 for _ in range(3)),
                                 media_type="multipart/x-mixed-replace; boundary=frame")

    @app.get("/download")
    def download():
        return FileResponse(Path(__file__), media_type="application/octet-stream", filename="evidence.zip")

    @app.get("/encoded")
    def encoded():
        return Response(gzip.compress(json.dumps(BIG).encode()), media_type="application/json", headers={"Content-Encoding": "gzip"})

    @app.websocket("/socket")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_bytes(b"\x00" * 50_000)
        await websocket.close()

    app.add_middleware(JsonCompression)
    return app


class JsonCompressionTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(stand_in())

    def encoding(self, path: str, method: str = "GET", headers: dict | None = None):
        response = self.client.request(method, path, headers=GZIP if headers is None else headers)
        return response, response.headers.get("content-encoding")

    def test_a_large_json_answer_to_a_get_is_compressed_for_a_browser_that_takes_gzip(self):
        response, coding = self.encoding("/json")
        self.assertEqual(coding, "gzip")
        self.assertEqual(response.json(), BIG)  # The client undoes it
        self.assertIn("Accept-Encoding", response.headers["vary"])
        raw = self.client.get("/json", headers=GZIP)
        self.assertLess(int(raw.headers["content-length"]), len(json.dumps(BIG)) // 5)

    def test_a_small_answer_or_a_client_that_does_not_take_gzip_gets_it_as_it_is(self):
        self.assertIsNone(self.encoding("/small")[1])
        self.assertIsNone(self.encoding("/json", headers={"Accept-Encoding": "identity"})[1])
        self.assertIsNone(self.encoding("/json", headers={"Accept-Encoding": "gzip;q=0"})[1])

    def test_an_answer_to_a_post_or_one_marked_as_holding_a_secret_is_never_compressed(self):
        for method, path in (("POST", "/json"), ("GET", "/secret")):
            with self.subTest(path=f"{method} {path}"):
                response, coding = self.encoding(path, method)
                self.assertIsNone(coding)
                self.assertEqual(response.json()["secret"], "s" * 40)

    def test_video_picture_streams_line_streams_downloads_and_streamed_json_are_untouched(self):
        for path in ("/video", "/mjpeg", "/lines", "/download", "/stream"):
            with self.subTest(path=path):
                response, coding = self.encoding(path)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(coding)
        self.assertEqual(self.encoding("/lines")[0].text.count("\n"), 200)

    def test_an_answer_already_encoded_is_not_encoded_again(self):
        response, coding = self.encoding("/encoded")
        self.assertEqual(coding, "gzip")
        self.assertEqual(response.json(), BIG)

    def test_a_websocket_is_untouched(self):
        with self.client.websocket_connect("/socket", headers=GZIP) as socket:
            self.assertEqual(socket.receive_bytes(), b"\x00" * 50_000)

    def test_the_application_compresses_json_answers(self):
        from app.application import create_app

        stack = [layer.cls for layer in create_app().user_middleware]
        self.assertIn(JsonCompression, stack)

    def test_the_answers_that_show_a_secret_once_are_all_answers_to_posts(self):
        """Only GETs are compressed. Each answer that shows a secret once is to a POST or PUT; the GET on the same path, where there is one,
        lists what exists (names, scopes), never the secret. If one of these ever became a GET, it would need ``no_compression``."""
        from app.main import app
        from app.routes import all_routes

        # A new API token, webhook and heartbeat signing secrets, second-factor enrolment, recovery codes, a passkey's registration,
        # a native device's sign-in, a discovered camera's claim
        secret_answers = {("POST", "/api/admin/api-tokens"), ("POST", "/api/webhooks"), ("POST", "/api/webhooks/{webhook_id}/rotate-secret"),
                          ("PUT", "/api/heartbeat"), ("POST", "/api/heartbeat/rotate-secret"), ("POST", "/api/me/security/totp/begin"),
                          ("POST", "/api/me/security/totp/confirm"), ("POST", "/api/me/security/recovery-codes"), ("POST", "/api/me/security/passkeys"),
                          ("POST", "/api/native/devices"), ("POST", "/api/native/device-sessions/resume"),
                          ("POST", "/api/discovery/candidates/{candidate_id}/add")}
        served: dict[tuple[str, str], object] = {}
        for route in all_routes(app):
            for method in getattr(route, "methods", None) or ():
                served[(method, getattr(route, "path", ""))] = route
        for method, path in sorted(secret_answers):
            with self.subTest(path=f"{method} {path}"):
                self.assertIn((method, path), served, "not served any more: update this list")
                self.assertNotEqual(method, "GET")

    def test_the_processor_cost_of_the_200_camera_list(self):
        body = json.dumps({"cameras": [{"id": f"{index:08x}-0000-0000-0000-000000000000", "name": f"Camera {index}", "recording_health": {"state": "recording",
                           "detail": "Recording as its policy expects"}, "camera_ip": f"192.0.2.{index % 250}", "group_name": "Dock"} for index in range(200)]}).encode()
        import time

        took = min(self._time(lambda: gzip.compress(body, compresslevel=compression.LEVEL, mtime=0), time) for _ in range(5))
        print(f"\n200-camera list: {len(body)} bytes, {len(gzip.compress(body, compresslevel=compression.LEVEL))} compressed, {took * 1000:.2f} ms")
        self.assertLess(took, 0.02)

    @staticmethod
    def _time(work, time) -> float:
        started = time.perf_counter()
        work()
        return time.perf_counter() - started


class PrecompressedAssetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.dist = Path(self.temporary.name)
        assets = self.dist / "assets"
        assets.mkdir(parents=True)
        (self.dist / "index.html").write_text("<!doctype html><title>NVR</title>", encoding="utf-8")
        self.script = ("const camera = 'Camera';\n" * 4000).encode()
        (assets / "index-abc123.js").write_bytes(self.script)
        (assets / "index-abc123.js.gz").write_bytes(gzip.compress(self.script))
        (assets / "index-abc123.js.br").write_bytes(b"brotli stand-in")
        patcher = patch.object(web_interface, "DIST_DIRECTORY", self.dist)
        patcher.start()
        self.addCleanup(patcher.stop)
        from app.main import app

        self.client = TestClient(app)

    def test_the_build_time_copy_is_served_to_a_browser_that_takes_it(self):
        gz = self.client.get("/assets/index-abc123.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual((gz.headers["content-encoding"], gz.content), ("gzip", self.script))
        self.assertTrue(gz.headers["content-type"].startswith(("text/javascript", "application/javascript")))
        self.assertIn("Accept-Encoding", gz.headers["vary"])
        self.assertIn("immutable", gz.headers["cache-control"])
        raw = self.client.get("/assets/index-abc123.js", headers={"Accept-Encoding": "br, gzip"}, )
        self.assertEqual(raw.headers["content-encoding"], "br")  # Brotli first when the browser takes it

    def test_a_browser_that_takes_neither_gets_the_file_itself(self):
        for header in ("identity", "gzip;q=0", ""):
            with self.subTest(header=header):
                plain = self.client.get("/assets/index-abc123.js", headers={"Accept-Encoding": header})
                self.assertIsNone(plain.headers.get("content-encoding"))
                self.assertEqual(plain.content, self.script)


if __name__ == "__main__":
    unittest.main()
