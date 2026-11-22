import os
import unittest
from unittest.mock import patch

from app.entrypoint import health_url, listen_host, listen_port


class EntrypointTests(unittest.TestCase):
    def test_default_listener_matches_host_network_compose(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(listen_port(), 8280)

    def test_listener_can_move_away_from_a_host_port_conflict(self):
        with patch.dict(os.environ, {"NVR_LISTEN_PORT": "18280"}, clear=True):
            self.assertEqual(listen_port(), 18280)

    def test_listener_rejects_invalid_ports(self):
        for value in ("zero", "0", "65536"):
            with self.subTest(value=value), patch.dict(os.environ, {"NVR_LISTEN_PORT": value}, clear=True):
                with self.assertRaises(SystemExit):
                    listen_port()


class ListenHostTests(unittest.TestCase):
    """#541: the interface can be served on one address, so it is not also open on the camera network and beside a tunnel."""

    def test_every_address_unless_one_is_named_and_the_health_check_follows(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual((listen_host(), health_url()), ("0.0.0.0", "http://127.0.0.1:8280/healthz"))
        for value, host, asked in (("127.0.0.1", "127.0.0.1", "http://127.0.0.1:18280/healthz"), (" 192.0.2.10 ", "192.0.2.10", "http://192.0.2.10:18280/healthz"),
                                   ("::1", "::1", "http://[::1]:18280/healthz"), ("0.0.0.0", "0.0.0.0", "http://127.0.0.1:18280/healthz"), ("", "0.0.0.0", "http://127.0.0.1:18280/healthz")):
            with self.subTest(value=value), patch.dict(os.environ, {"NVR_LISTEN_HOST": value, "NVR_LISTEN_PORT": "18280"}, clear=True):
                self.assertEqual((listen_host(), health_url()), (host, asked))

    def test_anything_that_is_not_an_address_stops_start_up_with_a_plain_message(self):
        from app import entrypoint

        for value in ("localhost", "nvr.example.test", "192.0.2.300", "192.0.2.0/24", "all", "127.0.0.1:8280"):
            with self.subTest(value=value), patch.dict(os.environ, {"NVR_LISTEN_HOST": value}, clear=True):
                with self.assertRaises(SystemExit) as stopped:
                    listen_host()
                self.assertIn("NVR_LISTEN_HOST must be one IP address", str(stopped.exception))
                with self.assertRaises(SystemExit):  # Before anything is started
                    entrypoint.main()

    def test_bound_to_this_machine_it_answers_here_and_refuses_everywhere_else(self):
        import socket
        import subprocess
        import sys
        import tempfile
        import time
        import urllib.request
        from pathlib import Path

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:  # An address of this machine that is not loopback, if it has one
            try:
                probe.connect(("192.0.2.1", 9))
                other = probe.getsockname()[0]
            except OSError:
                other = None
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "NVR_DB_PATH": f"{directory}/nvr.sqlite3", "NVR_RECORDINGS_PATH": f"{directory}/recordings", "NVR_BACKUP_PATH": f"{directory}/backups",
                           "NVR_CREDENTIAL_KEY_PATH": f"{directory}/credentials.key", "NVR_LISTEN_PORT": str(port), "NVR_LISTEN_HOST": "127.0.0.1"}
            server = subprocess.Popen([sys.executable, "-m", "app.workers.web"], env=environment, cwd=Path(__file__).resolve().parents[1],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 60
                while True:
                    try:
                        self.assertEqual(direct.open(f"http://127.0.0.1:{port}/healthz", timeout=2).status, 200)
                        break
                    except OSError:
                        self.assertLess(time.monotonic(), deadline, "the web worker did not start answering")
                        self.assertIsNone(server.poll(), "the web worker stopped")
                        time.sleep(0.2)
                # The container's own health check asks at the bound address and passes.
                check = subprocess.run([sys.executable, "-m", "app.healthcheck"], env=environment, cwd=Path(__file__).resolve().parents[1], capture_output=True, check=False)
                self.assertEqual(check.returncode, 0, check.stderr)
                if other is None or other.startswith("127."):
                    self.skipTest("this machine has no address besides loopback to be refused on")
                with self.assertRaises(ConnectionRefusedError):
                    socket.create_connection((other, port), timeout=3).close()
            finally:
                server.terminate()
                server.wait(timeout=20)

    def test_the_tunnel_overlays_serve_this_machine_only_and_the_health_checks_ask_where_it_listens(self):
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parents[1]
        for overlay in ("compose.tailscale.yaml", "compose.cloudflared.yaml"):
            environment = yaml.safe_load((root / overlay).read_text(encoding="utf-8"))["services"]["nvr"]["environment"]
            self.assertEqual(environment["NVR_LISTEN_HOST"], "${NVR_LISTEN_HOST:-127.0.0.1}", overlay)
        base = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))["services"]["nvr"]
        self.assertIn("NVR_LISTEN_HOST", base["environment"])
        self.assertIsNone(base["environment"]["NVR_LISTEN_HOST"])  # The plain stack: every address unless the installer names one
        self.assertEqual(base["healthcheck"]["test"], ["CMD", "python", "-m", "app.healthcheck"])
        self.assertIn("CMD python -m app.healthcheck", (root / "Dockerfile").read_text(encoding="utf-8"))

    def test_the_hardening_page_is_told_when_the_interface_is_served_on_every_address(self):
        from app import hardening

        with patch.dict(os.environ, {"NVR_LISTEN_HOST": ""}):
            every = hardening.listening()
        self.assertEqual((every["host"], every["every_address"]), ("0.0.0.0", True))
        self.assertIsInstance(every["networks"], list)
        self.assertFalse([entry for entry in every["networks"] if entry.startswith("127.")])
        with patch.dict(os.environ, {"NVR_LISTEN_HOST": "127.0.0.1"}):
            self.assertEqual(hardening.listening(), {"host": "127.0.0.1", "every_address": False, "networks": []})


if __name__ == "__main__":
    unittest.main()
