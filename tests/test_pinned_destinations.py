"""Outbound requests connect to the address that was checked, not to whatever the name says the second time it is looked up (#531).

The experiment from the ticket: a name whose lookup answers an outside address the first time and 127.0.0.1 the second, and a listener on
loopback. The check judged the first answer; the HTTP client looked the name up again and connected to the second. Now the request goes to
the checked address, with the name kept for the Host header and for TLS.
"""
from __future__ import annotations

import asyncio
import datetime
import os
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app import heartbeat, push, rule_actions, webhooks
from app.credentials import encrypt
from app.store import connect, initialize

OUTSIDE = "203.0.113.9"  # The first answer: a documentation address, never reachable
NAME = "hooks.example.test"
REAL_GETADDRINFO = socket.getaddrinfo


class Rebinding:
    """A resolver that answers the name with OUTSIDE once and 127.0.0.1 after; addresses written out are left alone."""

    def __init__(self, names: set[str]):
        self.names, self.asked = names, 0

    def __call__(self, host, port, *args, **kwargs):
        name = (host.decode() if isinstance(host, bytes) else str(host)).rstrip(".")  # The HTTP client asks with bytes
        if name not in self.names:
            return REAL_GETADDRINFO(host, port, *args, **kwargs)
        self.asked += 1
        address = OUTSIDE if self.asked == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, int(port or 443)))]


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        environment = mock.patch.dict(os.environ, {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_CREDENTIAL_KEY_PATH": str(root / "key")})
        environment.start()
        self.addCleanup(environment.stop)
        initialize()
        self.reached = 0

        async def listener(reader, writer):
            self.reached += 1
            writer.close()

        self.server = await asyncio.start_server(listener, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(self._close)
        # The ticket's experiment judges only the address lookups, so the policy is set aside: OUTSIDE is a documentation address
        for patch in (mock.patch.object(webhooks, "_check_destination_policy", lambda *args, **kwargs: None),
                      mock.patch.object(rule_actions, "ATTEMPT_DEADLINE_SECONDS", 2.0),
                      mock.patch.object(webhooks, "ATTEMPT_DEADLINE_SECONDS", 2.0)):
            patch.start()
            self.addCleanup(patch.stop)

    async def _close(self):
        self.server.close()

    def url(self, name: str = NAME) -> str:
        return f"https://{name}:{self.port}/in"


class PathTests(Case):
    async def test_a_webhook_connects_to_the_checked_address_only(self):
        resolver = Rebinding({NAME})
        endpoint = "e1"
        with connect() as db:
            db.execute("INSERT INTO webhook_endpoints(id,name,target_url_ciphertext,signing_secret_ciphertext,events_json,allow_private,enabled,created_at,updated_at) "
                       "VALUES(?,?,?,?,'[\"ai.detection\"]',0,1,?,?)", (endpoint, "Hooks", encrypt(self.url()), encrypt("s" * 32), webhooks._now(), webhooks._now()))
            webhooks.enqueue_event(db, "ai.detection", {"event_id": "x1", "event_type": "ai.detection"})
        manager = webhooks.WebhookManager()
        with mock.patch("socket.getaddrinfo", resolver):
            await manager._deliver(webhooks.claim_due_delivery())
        await manager.stop()
        self.assertEqual(self.reached, 0)

    async def test_a_rule_action_connects_to_the_checked_address_only(self):
        resolver = Rebinding({NAME})
        with mock.patch("socket.getaddrinfo", resolver):
            with self.assertRaises(Exception):  # The checked address cannot be reached; what matters is where it went
                await rule_actions._post(self.url(), b"{}", {"Content-Type": "application/json"}, False)
        self.assertEqual(self.reached, 0)

    async def test_the_heartbeat_connects_to_the_checked_address_only(self):
        resolver = Rebinding({NAME})
        config = {"target_url_ciphertext": encrypt(self.url()), "signing_secret_ciphertext": encrypt("s" * 32), "allow_private": False,
                  "instance_id": "nvr", "test": False}
        with mock.patch("socket.getaddrinfo", resolver), mock.patch.object(heartbeat, "record_heartbeat_result", lambda **kwargs: None), \
             mock.patch.object(heartbeat, "REQUEST_TIMEOUT_SECONDS", 1.0):
            await heartbeat.HeartbeatManager()._send(config)
        self.assertEqual(self.reached, 0)

    async def test_a_push_notification_connects_to_the_checked_address_only(self):
        service = "fcm.googleapis.com"
        resolver = Rebinding({service})
        with mock.patch("socket.getaddrinfo", resolver), mock.patch.object(push, "SEND_DEADLINE_SECONDS", 2.0):
            pinned = push.pin_endpoint(self.url(service))
            with httpx.Client(timeout=httpx.Timeout(1.0), trust_env=False) as client, self.assertRaises(Exception):
                await asyncio.to_thread(push._post_for_status, client, pinned, b"x", {})
        self.assertEqual(self.reached, 0)
        self.assertEqual((pinned.host, pinned.sni, httpx.URL(pinned.url).host), (f"{service}:{self.port}", service, OUTSIDE))

    async def test_the_name_is_looked_up_once_and_kept_for_the_host_header_and_tls(self):
        resolver = Rebinding({NAME})
        with mock.patch("socket.getaddrinfo", resolver):
            pinned = webhooks.pin_target_url(self.url(), False)
        self.assertEqual(resolver.asked, 1)
        arguments = pinned.request({"X-Test": "1"})
        self.assertEqual(httpx.URL(arguments["url"]).host, OUTSIDE)
        self.assertEqual(arguments["headers"], {"X-Test": "1", "Host": f"{NAME}:{self.port}"})
        self.assertEqual(arguments["extensions"], {"sni_hostname": NAME})

    async def test_a_literal_address_needs_no_name_for_tls(self):
        pinned = webhooks.pin_target_url("http://192.0.2.44:8080/hook", True)
        self.assertEqual((pinned.url, pinned.host, pinned.sni, pinned.request()["extensions"]), ("http://192.0.2.44:8080/hook", "192.0.2.44:8080", None, {}))


class TlsTests(unittest.TestCase):
    """Connecting by address still checks the certificate against the name."""

    def setUp(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NAME)])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(1)
                       .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName(NAME)]), critical=False)
                       .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
        (root / "cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        (root / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(root / "cert.pem", root / "key.pem")
        self.trust = ssl.create_default_context(cafile=str(root / "cert.pem"))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.port = listener.getsockname()[1]
        self.addCleanup(listener.close)

        def serve():
            while True:
                try:
                    raw, _ = listener.accept()
                except OSError:
                    return
                try:
                    with server_context.wrap_socket(raw, server_side=True) as tls:
                        tls.recv(65536)
                        tls.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except (ssl.SSLError, OSError):
                    pass

        threading.Thread(target=serve, daemon=True).start()

    def send(self, sni: str) -> int:
        pinned = webhooks.Pinned(url=f"https://127.0.0.1:{self.port}/in", host=f"{NAME}:{self.port}", sni=sni)
        with httpx.Client(verify=self.trust, trust_env=False, timeout=5) as client:
            return client.post(content=b"{}", **pinned.request()).status_code

    def test_the_certificate_is_checked_against_the_name_not_the_address(self):
        self.assertEqual(self.send(NAME), 204)
        with self.assertRaises(httpx.ConnectError):  # The same server, asked as another name: the certificate does not match
            self.send("other.example.test")


if __name__ == "__main__":
    unittest.main()
