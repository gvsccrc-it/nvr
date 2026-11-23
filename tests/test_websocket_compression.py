"""Live video is not run through WebSocket compression that cannot shrink it (#545).

uvicorn offers ``permessage-deflate`` by default and browsers ask for it, so every piece of live video was deflated on the web process's event
loop for nothing (video is already compressed). These start the real application under uvicorn with the web worker's own settings and offer
the extension in the handshake, as a browser does."""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import uvicorn

from app.workers import web

OFFER = "permessage-deflate; client_max_window_bits"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        patcher = patch.dict(os.environ, {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(root / "recordings"),
                                          "NVR_CREDENTIAL_KEY_PATH": str(root / "key"), "NVR_CONTROL_DIR": str(root / "run")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, **override) -> int:
        """The real application under uvicorn, with the web worker's settings (and ``override``), on a free local port."""
        port = free_port()
        settings = {**web.server_settings(), "host": "127.0.0.1", "port": port, "log_config": None, **override}
        server = uvicorn.Server(uvicorn.Config("app.main:app", **settings))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 15)
        self.addCleanup(setattr, server, "should_exit", True)
        deadline = time.monotonic() + 30
        while not server.started:
            self.assertTrue(thread.is_alive(), "the server did not start")
            self.assertLess(time.monotonic(), deadline, "the server did not start in time")
            time.sleep(0.05)
        return port

    def handshake(self, port: int, path: str) -> tuple[str, dict[str, str]]:
        """Open a WebSocket offering compression, as a browser does: the status line and the answer's headers."""
        request = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Extensions: {OFFER}\r\n\r\n")
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            client.sendall(request.encode())
            answer = b""
            while b"\r\n\r\n" not in answer:
                piece = client.recv(4096)
                if not piece:
                    break
                answer += piece
            client.settimeout(1.0)
            try:  # Read on until the server closes (it refuses a viewer who is not signed in), so it is not cut off mid-reply
                while client.recv(4096):
                    pass
            except OSError:
                pass
        status, *lines = answer.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        return status, {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines)}

    def test_the_live_socket_does_not_accept_compression_and_the_other_sockets_still_open(self):
        port = self.serve()
        camera = str(uuid.uuid4())
        for path in (f"/api/cameras/{camera}/live/stream", f"/api/cameras/{camera}/talk/stream"):
            with self.subTest(path=path):
                status, headers = self.handshake(port, path)
                self.assertIn("101", status, f"{path}: {status}")
                self.assertNotIn("permessage-deflate", headers.get("sec-websocket-extensions", ""))

    def test_the_same_handshake_is_given_compression_when_the_server_offers_it(self):
        # The control: uvicorn's default does accept what this handshake offers, so the test above shows the setting, not a handshake that never asked
        port = self.serve(ws_per_message_deflate=True)
        status, headers = self.handshake(port, f"/api/cameras/{uuid.uuid4()}/live/stream")
        self.assertIn("101", status)
        self.assertIn("permessage-deflate", headers.get("sec-websocket-extensions", ""))

    def test_the_web_worker_runs_with_these_settings(self):
        with patch.object(web.uvicorn, "run") as run, patch.object(web.logforward, "install"), patch.object(web.db_health, "install_log_filter"), \
                patch.object(web.open_files, "raise_limit"):
            web.main()
        self.assertEqual(run.call_args.args, ("app.main:app",))
        self.assertIs(run.call_args.kwargs["ws_per_message_deflate"], False)
        self.assertEqual(run.call_args.kwargs["http"], "app.connection_guard:GuardedProtocol")


if __name__ == "__main__":
    unittest.main()
