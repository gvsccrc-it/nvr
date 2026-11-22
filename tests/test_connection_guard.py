"""Idle connections cannot use up the web process, and every worker raises its own open-file limit (#546).

A real uvicorn server runs here with the NVR's connection rules and a tiny application, and real sockets are opened against it: the
rules are about what happens before a request exists, which no test client reaches."""
from __future__ import annotations

import asyncio
import os
import resource
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn
import yaml

from app import connection_guard, open_files

ROOT = Path(__file__).resolve().parents[1]


async def tiny(scope, receive, send):
    """Answers every request; /slow takes its time, as a request doing real work does."""
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": "hello"})
        await asyncio.sleep(4)  # Open, as a live video socket stays open, for longer than the bound on a first request
        await send({"type": "websocket.close"})
        return
    if scope["type"] != "http":
        return
    if scope["path"] == "/slow":
        await asyncio.sleep(3)
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


class Served(unittest.TestCase):
    LIMIT, FIRST_REQUEST_SECONDS = "20", "2"

    def setUp(self):
        self.environment = patch.dict(os.environ, {"NVR_WEB_MAX_CONNECTIONS": self.LIMIT, "NVR_WEB_FIRST_REQUEST_SECONDS": self.FIRST_REQUEST_SECONDS})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        connection_guard.GuardedProtocol.waiting.clear()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.port = listener.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(tiny, http="app.connection_guard:GuardedProtocol", log_level="error", access_log=False, timeout_keep_alive=5))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [listener]}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 10
        while not self.server.started:
            self.assertLess(time.monotonic(), deadline, "the server did not start")
            time.sleep(0.02)
        self.opened: list[socket.socket] = []

    def stop(self):
        for held in self.opened:
            held.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def idle(self) -> socket.socket:
        """A connection that sends nothing."""
        held = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.opened.append(held)
        return held

    def ask(self, path: str = "/healthz", timeout: float = 5) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as asking:
            asking.sendall(f"GET {path} HTTP/1.1\r\nHost: nvr\r\nConnection: close\r\n\r\n".encode())
            answer = b""
            while chunk := asking.recv(4096):
                answer += chunk
        return answer

    @staticmethod
    def closed_by_the_server(held: socket.socket, within: float) -> bool:
        held.settimeout(within)
        try:
            return held.recv(1) == b""
        except (TimeoutError, socket.timeout):
            return False
        except OSError:
            return True


class IdleConnectionTests(Served):
    def test_a_connection_that_sends_no_request_is_closed_after_the_bound_and_one_that_did_is_not_cut_off(self):
        began = time.monotonic()
        silent = self.idle()
        half = self.idle()
        half.sendall(b"GET /healthz HTTP/1.1\r\nHost: nvr\r\n")  # A request line and a header, never finished: still not a request
        working = self.idle()
        working.sendall(b"GET /slow HTTP/1.1\r\nHost: nvr\r\nConnection: close\r\n\r\n")  # A whole request that takes longer than the bound
        self.assertFalse(self.closed_by_the_server(silent, 0.5), "closed before the bound")
        self.assertTrue(self.closed_by_the_server(silent, 5))
        self.assertTrue(self.closed_by_the_server(half, 5))
        self.assertGreaterEqual(time.monotonic() - began, 1.9)
        working.settimeout(10)
        answer = b""
        while chunk := working.recv(4096):
            answer += chunk
        self.assertTrue(answer.startswith(b"HTTP/1.1 200"), answer[:80])  # Answered after 3 s: the bound is on the first request arriving, not on the work
        self.assertEqual(connection_guard.GuardedProtocol.waiting, {})

    def test_a_live_socket_connects_through_the_rules_stays_open_past_the_bound_and_is_counted(self):
        live = self.idle()
        live.sendall(b"GET /live HTTP/1.1\r\nHost: nvr\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
        live.settimeout(5)
        answer = live.recv(4096)
        self.assertTrue(answer.startswith(b"HTTP/1.1 101"), answer[:80])
        self.assertEqual(connection_guard.GuardedProtocol.waiting, {})  # It asked: no longer waiting for a first request
        self.assertEqual(len(self.server.server_state.connections), 1)  # And still one of the connections the limit counts
        time.sleep(float(self.FIRST_REQUEST_SECONDS) + 0.5)
        live.setblocking(False)
        try:
            while live.recv(4096) != b"":  # What the socket sent so far ("hello"); an empty read would be the server closing it
                pass
            self.fail("a live socket was closed as if it had sent nothing")
        except BlockingIOError:
            pass  # Nothing more to read and not closed: still open

    def test_a_flood_of_idle_connections_does_not_stop_the_health_check_answering(self):
        for _ in range(60):  # Three times the limit, from one machine, sending nothing
            self.idle()
        for _ in range(5):
            self.assertTrue(self.ask().startswith(b"HTTP/1.1 200"))
        time.sleep(0.2)
        # Never more held than the limit: each newcomer took the place of the idle connection that had waited longest
        self.assertLessEqual(len(self.server.server_state.connections), int(self.LIMIT))
        still_open = [held for held in self.opened if not self.closed_by_the_server(held, 0.01)]
        self.assertLessEqual(len(still_open), int(self.LIMIT))
        self.assertTrue(self.closed_by_the_server(self.opened[0], 0.5), "the oldest idle connection was not the one that made way")
        # And after the bound none of them is left
        time.sleep(float(self.FIRST_REQUEST_SECONDS) + 0.5)
        self.assertEqual(len(self.server.server_state.connections), 0)

    def test_when_every_connection_is_working_a_stranger_is_turned_away_and_this_machine_still_gets_in(self):
        for _ in range(int(self.LIMIT)):
            self.idle().sendall(b"GET /slow HTTP/1.1\r\nHost: nvr\r\nConnection: close\r\n\r\n")
        time.sleep(0.3)
        self.assertEqual(connection_guard.GuardedProtocol.waiting, {})  # All of them asked: nothing idle to displace
        self.assertTrue(self.ask().startswith(b"HTTP/1.1 200"))  # From this machine, as the health check is: let in over the limit
        with patch.object(connection_guard, "_this_machine", return_value=False):  # The same connection from anywhere else
            with self.assertRaises((ConnectionError, AssertionError)):
                self.assertNotEqual(self.ask(timeout=2), b"")  # Closed without an answer
        self.assertGreaterEqual(connection_guard.GuardedProtocol.counts["refused"], 1)


class LimitsTests(unittest.TestCase):
    def test_a_process_with_a_lowered_limit_raises_it_as_far_as_the_system_allows_and_never_lowers_it(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        if hard == resource.RLIM_INFINITY or hard <= 512:
            self.skipTest("this system has no hard limit above 512 to raise to")
        resource.setrlimit(resource.RLIMIT_NOFILE, (512, hard))
        self.assertEqual(open_files.raise_limit(), hard)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_NOFILE), (hard, hard))
        self.assertEqual(open_files.raise_limit(), hard)  # Already there: nothing to do

    def test_every_worker_and_the_supervisor_raise_their_limit_through_the_one_helper(self):
        for path in ("app/workers/runtime.py", "app/workers/web.py", "app/workers/supervisor.py", "app/exports.py"):
            self.assertIn("open_files.raise_limit()", (ROOT / path).read_text(encoding="utf-8"), path)
        # The recorder, exporter, AI, rules and background workers all start through the shared runtime
        for worker in ("recorder", "exporter", "ai", "rules", "background"):
            self.assertIn("runtime.main(", (ROOT / "app/workers" / f"{worker}.py").read_text(encoding="utf-8"), worker)
        self.assertEqual([path.name for path in (ROOT / "app").rglob("*.py") if "RLIMIT_NOFILE" in path.read_text(encoding="utf-8")], ["open_files.py"])

    def test_the_web_worker_runs_with_the_connection_rules(self):
        from app.workers import web

        with patch.object(web.uvicorn, "run") as run, patch.object(web.logforward, "install"), patch.object(web.db_health, "install_log_filter"):
            web.main()
        self.assertEqual(run.call_args.kwargs["http"], "app.connection_guard:GuardedProtocol")
        self.assertEqual((connection_guard.max_connections(), connection_guard.first_request_seconds()), (512, 10.0))

    def test_a_connection_from_the_address_it_arrived_at_counts_as_this_machine(self):
        this = connection_guard._this_machine
        self.assertTrue(this(("127.0.0.1", 40000), ("127.0.0.1", 8280)))
        self.assertTrue(this(("::1", 40000), ("::", 8280)))
        self.assertTrue(this(("192.0.2.10", 40000), ("192.0.2.10", 8280)))  # The health check, with the interface served on one address
        self.assertFalse(this(("192.0.2.77", 40000), ("192.0.2.10", 8280)))
        self.assertFalse(this(None, ("192.0.2.10", 8280)))

    def test_the_compose_file_sets_the_open_file_limit(self):
        service = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]["nvr"]
        self.assertEqual(service["ulimits"]["nofile"], {"soft": 65536, "hard": 65536})
        # No process limit yet, on purpose: the 4,096 first decided on #546 is fewer threads than 200 cameras with server-side motion
        # detection run (about 21 a detector, measured). It waits for the owner's answer on the ticket.
        self.assertNotIn("pids_limit", service)


if __name__ == "__main__":
    unittest.main()
