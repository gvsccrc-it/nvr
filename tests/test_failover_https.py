"""The failover link over HTTPS, each NVR pinning the other's certificate (#502, part 2; the owner's decision of 24 November 2026).

- Each NVR makes its own certificate for the link once, keeps it beside its credential key with the private key encrypted under that key,
  and never shows the key in a log, an answer or the audit log.
- Pairing records the other NVR's fingerprint; a connection to the other NVR checks the certificate it presents against it during the
  handshake, before anything is sent, and refuses a different one with a clear message and an incident (a real TLS server here, with
  certificates made for the test in a temporary folder).
- Plain HTTP works only with "Allow an unencrypted link" ticked on both: this NVR neither sends to an http:// address nor answers a request
  that arrived unencrypted unless its own box is ticked.
- A pair set up before the upgrade keeps working as it was, and the hardening page lists it.
- Two real NVR processes pair over their HTTPS links, renew the lease and copy the configuration, and refuse a wrong certificate.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from app import failover, failover_link, hardening, migrations
from app.credentials import decrypt_bytes
from app.failover_listener import FailoverLink
from app.store import connect, database_path, initialize
from tests.fixtures.nvr_site import PASSWORD, ROOT, NvrSite
from tests.test_failover import LINK, PASSPHRASE, SECRET, FailoverCase

LEASE = "/api/inbound/failover/lease"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def throwaway_certificate(directory: Path, name: str) -> tuple[Path, Path, str]:
    """A self-signed certificate and its key, made for one test in a temporary folder (nothing of it is kept in the repository)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=2)).sign(key, hashes.SHA256()))
    certificate_file, key_file = directory / f"{name}.crt", directory / f"{name}.key"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return certificate_file, key_file, hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()


class TlsServer:
    """A real TLS server on this machine that notes every request that reaches it."""

    def __init__(self, certificate_file: Path, key_file: Path) -> None:
        self.seen: list[str] = []

        async def app(scope, receive, send):
            if scope["type"] != "http":
                return
            self.seen.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"answered"})

        self.socket = socket.create_server(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_config=None, access_log=False, ssl_certfile=str(certificate_file), ssl_keyfile=str(key_file)))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)

    def __enter__(self) -> "TlsServer":
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert self.server.started, "The test's TLS server did not start"
        return self

    def __exit__(self, *_exc) -> None:
        self.server.should_exit = True
        self.thread.join(10)

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"


class Collected(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage() + (self.formatException(record.exc_info) if record.exc_info else ""))


def eventually(check, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(message)


class CertificateTests(FailoverCase):
    def test_the_certificate_is_made_once_and_kept(self):
        pem, fingerprint = failover_link.certificate()
        self.assertTrue(pem.startswith(b"-----BEGIN CERTIFICATE-----"))
        made = {path: path.stat().st_mtime_ns for path in (failover_link.certificate_path(), failover_link.key_file_path())}
        failover_link._known.clear()  # As another process, or after a restart, would find it
        self.assertEqual(failover_link.certificate(), (pem, fingerprint))
        self.assertEqual({path: path.stat().st_mtime_ns for path in made}, made)  # Not made again
        self.assertEqual(failover_link.certificate_path().parent, Path(os.environ["NVR_CREDENTIAL_KEY_PATH"]).parent)  # With the NVR's other keys
        self.assertEqual(fingerprint, hashlib.sha256(x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)).hexdigest())
        # The fingerprint the page shows is the one openssl and a browser show for the certificate
        shown = self.admin.get("/api/admin/failover").json()["link"]
        self.assertEqual(shown["fingerprint"], failover_link.display(fingerprint))
        self.assertRegex(shown["fingerprint"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertEqual(shown["port"], failover_link.DEFAULT_PORT)

    def test_the_private_key_is_encrypted_where_it_is_kept(self):
        pem, _ = failover_link.certificate()
        stored = failover_link.key_file_path().read_bytes()
        self.assertNotIn(b"PRIVATE KEY", stored)
        self.assertTrue(stored.startswith(b"gAAAA"))  # A token sealed with the credential key
        for path in (failover_link.key_file_path(), failover_link.certificate_path()):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        key = serialization.load_pem_private_key(decrypt_bytes(stored), password=None)
        self.assertEqual(key.public_key().public_numbers(), x509.load_pem_x509_certificate(pem).public_key().public_numbers())
        # Serving it leaves nothing behind: the key is handed to TLS through a file sealed with a one-time password, then removed
        before = set(failover_link.directory().iterdir())
        failover_link.server_context()
        self.assertEqual(set(failover_link.directory().iterdir()), before)

    def test_the_private_key_never_reaches_a_log_an_answer_or_the_audit_log(self):
        collected = Collected()
        root = logging.getLogger()
        level = root.level
        root.addHandler(collected)
        root.setLevel(logging.DEBUG)
        try:
            failover_link.certificate()
            first_key = decrypt_bytes(failover_link.key_file_path().read_bytes())
            failover_link.server_context()
            answers = [self.admin.get("/api/admin/failover").text, self.admin.post("/api/admin/failover/link-certificate").text, self.admin.get("/api/hardening").text]
            second_key = decrypt_bytes(failover_link.key_file_path().read_bytes())
            # A key that cannot be read with this NVR's credential key (another NVR's restored): a new certificate, and a warning without the key
            failover_link.key_file_path().write_bytes(b"gAAAAnot-a-token")
            failover_link._known.clear()
            failover_link.certificate()
        finally:
            root.removeHandler(collected)
            root.setLevel(level)
        with connect() as db:
            audit = [row[0] for row in db.execute("SELECT details_json FROM audit_log WHERE action LIKE 'failover%'")]
        self.assertTrue(any("could not be read with this NVR's credential key" in message for message in collected.messages))
        for key in (first_key, second_key):
            body = b"".join(key.splitlines()[1:-1]).decode()  # The key itself, without its PEM lines
            for text in [*collected.messages, *answers, *audit]:
                self.assertNotIn(body[:40], text)
                self.assertNotIn("PRIVATE KEY", text)
        self.assertEqual(len(audit), 1)
        self.assertIn("fingerprint", json.loads(audit[0]))

    def test_a_new_certificate_has_a_new_fingerprint_and_is_audited(self):
        _, before = failover_link.certificate()
        answer = self.admin.post("/api/admin/failover/link-certificate")
        self.assertEqual(answer.status_code, 200, answer.text)
        after = answer.json()["link"]["fingerprint"]
        self.assertNotEqual(after, failover_link.display(before))
        self.assertEqual(after, failover_link.display(failover_link.certificate()[1]))
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE action='failover_link_certificate_replaced'").fetchone()[0], 1)


class PairingTests(FailoverCase):
    PEER = "https://192.0.2.20:8443"

    def test_a_new_https_pair_needs_the_other_nvrs_fingerprint(self):
        """The owner's decision of 24 Nov: every new https:// pair pins the other NVR's certificate; no certificate authority is trusted."""
        base = {"role": "primary", "peer_url": self.PEER, "secret": SECRET, "silence_seconds": 12, "lease_seconds": 5, "sync_seconds": 60}
        refused = self.admin.put("/api/admin/failover", json=base)
        self.assertEqual(refused.status_code, 422)
        self.assertIn("fingerprint", refused.json()["detail"])
        with connect() as db:
            self.assertEqual(failover.read_settings(db)["role"], "off")  # Nothing was saved
        self.assertEqual(self.admin.put("/api/admin/failover", json={**base, "peer_fingerprint": "ab" * 32}).status_code, 200)

    def test_a_pair_saved_before_without_a_fingerprint_keeps_working_until_its_address_changes(self):
        self.configure("primary", peer_url=self.PEER)
        with connect() as db:  # As saved before fingerprints were required
            db.execute("UPDATE failover_settings SET peer_fingerprint=NULL")
        base = {"role": "primary", "peer_url": self.PEER, "silence_seconds": 15, "lease_seconds": 5, "sync_seconds": 60}
        self.assertEqual(self.admin.put("/api/admin/failover", json=base).status_code, 200, "re-saving an existing pair was refused")
        warning = self.admin.get("/api/hardening").json()["failover_link"]
        self.assertEqual((warning["unpinned"], warning["sends_unencrypted"], warning["accepts_unencrypted"]), (True, False, False))
        moved = self.admin.put("/api/admin/failover", json={**base, "peer_url": "https://192.0.2.21:8443"})
        self.assertEqual(moved.status_code, 422)  # A new address is a new pair
        self.assertEqual(self.admin.put("/api/admin/failover", json={**base, "peer_fingerprint": "ab" * 32}).status_code, 200)
        self.assertIsNone(self.admin.get("/api/hardening").json()["failover_link"])  # Pinned now: nothing to warn about

    def test_pairing_records_the_other_nvrs_fingerprint(self):
        fingerprint = hashlib.sha256(b"the other NVR's certificate").hexdigest()
        pasted = failover_link.display(fingerprint).lower()  # As copied from the other page, in either case
        view = self.configure("primary", peer_url=self.PEER, peer_fingerprint=pasted)
        self.assertEqual(view["settings"]["peer_fingerprint"], failover_link.display(fingerprint))
        self.assertEqual(view["status"]["link"], "pinned")
        with connect() as db:
            self.assertEqual(failover.read_settings(db)["peer_fingerprint"], fingerprint)
            details = json.loads(db.execute("SELECT details_json FROM audit_log WHERE action='failover_settings_changed' ORDER BY id DESC").fetchone()[0])
        self.assertEqual((details["peer_fingerprint"], details["allow_unencrypted"]), (failover_link.display(fingerprint), False))
        # Spaces and no colons are as good
        self.configure("primary", peer_url=self.PEER, peer_fingerprint=" ".join(fingerprint[index:index + 8] for index in range(0, 64, 8)))
        with connect() as db:
            self.assertEqual(failover.read_settings(db)["peer_fingerprint"], fingerprint)

    def test_a_fingerprint_that_is_not_one_or_is_this_nvrs_own_is_refused(self):
        base = {"role": "primary", "peer_url": self.PEER, "secret": SECRET, "silence_seconds": 12, "lease_seconds": 5, "sync_seconds": 60}
        for wrong in ("AB:CD", "z" * 64, "ab" * 33):
            answer = self.admin.put("/api/admin/failover", json={**base, "peer_fingerprint": wrong})
            self.assertEqual(answer.status_code, 422, wrong)
            self.assertIn("64 hexadecimal characters", answer.json()["detail"])
        own = failover_link.display(failover_link.certificate()[1])
        answer = self.admin.put("/api/admin/failover", json={**base, "peer_fingerprint": own})
        self.assertEqual((answer.status_code, "this NVR's own fingerprint" in answer.json()["detail"]), (422, True))
        answer = self.admin.put("/api/admin/failover", json={**base, "peer_url": "http://192.0.2.20:8280", "peer_fingerprint": "ab" * 32, "allow_unencrypted": True})
        self.assertEqual((answer.status_code, "https://" in answer.json()["detail"]), (422, True))

    def test_a_plain_http_address_needs_the_unencrypted_link_allowed(self):
        base = {"role": "primary", "peer_url": "http://192.0.2.20:8280", "secret": SECRET, "silence_seconds": 12, "lease_seconds": 5, "sync_seconds": 60}
        answer = self.admin.put("/api/admin/failover", json=base)
        self.assertEqual(answer.status_code, 422)
        self.assertIn("Allow an unencrypted link", answer.json()["detail"])
        view = self.configure("primary", peer_url="http://192.0.2.20:8280", allow_unencrypted=True)
        self.assertEqual((view["settings"]["allow_unencrypted"], view["status"]["link"]), (True, "unencrypted"))
        self.configure("off", peer_url=None, allow_unencrypted=True)
        with connect() as db:
            self.assertFalse(failover.read_settings(db)["allow_unencrypted"])  # Switched off, nothing is allowed


class PinningTests(FailoverCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.right = throwaway_certificate(Path(self.folder.name), "standby")
        self.other = throwaway_certificate(Path(self.folder.name), "someone-else")

    def fetch(self, url: str, fingerprint: str) -> httpx.Response:
        async def go():
            async with failover.link_client({"peer_url": url, "peer_fingerprint": fingerprint}, failover.TIMEOUT) as client:
                return await client.get(url + "/anything")
        return asyncio.run(go())

    def test_only_the_paired_certificate_is_trusted_and_a_different_one_gets_nothing(self):
        with TlsServer(*self.right[:2]) as server:
            self.assertEqual(self.fetch(server.url, self.right[2]).text, "answered")  # No certificate authority vouches for it: the pin does
            self.assertEqual(server.seen, ["/anything"])
            with self.assertRaises(failover_link.CertificateMismatch) as caught:
                self.fetch(server.url, self.other[2])
            self.assertEqual(server.seen, ["/anything"])  # Refused during the handshake: the request was never sent
            self.assertIn(failover_link.display(self.right[2]), str(caught.exception))
            self.assertIn(failover_link.display(self.other[2]), str(caught.exception))
            # Without a pin an https:// address is checked against the certificate authorities, as before: a self-signed one is refused
            with self.assertRaises(httpx.ConnectError):
                self.fetch(server.url, "")
            self.assertEqual(server.seen, ["/anything"])

    def test_the_primary_refuses_a_standby_presenting_another_certificate_says_why_and_raises_an_incident(self):
        with TlsServer(*self.other[:2]) as server:
            self.configure("primary", peer_url=server.url, peer_fingerprint=self.right[2])
            with connect() as db:
                settings = failover.read_settings(db)
            asyncio.run(failover.FailoverManager()._renew(settings, SECRET))
            self.assertEqual(server.seen, [])  # Nothing signed reached whoever answered
        state = self.state()
        self.assertEqual(state["peer_state"], "wrong_certificate")
        self.assertIn("not the one it was paired with", state["last_error"])
        self.assertIn("pair the two again", state["last_error"])
        with connect() as db:
            incident = db.execute("SELECT * FROM incidents WHERE incident_key=?", (failover.CERTIFICATE_KEY,)).fetchone()
            self.assertEqual((incident["state"], incident["fault_type"]), ("active", "failover"))
            self.assertIn("certificate", failover.status_view(db)["headline"])
            self.assertIsNone(failover.held_reason(db))  # The primary goes on recording
        # The next answer it can verify recovers the incident
        failover.FailoverManager._apply_answer(settings, datetime.now(timezone.utc), {"granted": True, "state": "watching", "epoch": 0, "handback": None})
        with connect() as db:
            self.assertEqual(db.execute("SELECT state FROM incidents WHERE incident_key=?", (failover.CERTIFICATE_KEY,)).fetchone()[0], "recovered")

    def test_a_standby_refuses_a_primary_presenting_another_certificate(self):
        with TlsServer(*self.other[:2]) as server:
            self.configure("standby", peer_url=server.url, peer_fingerprint=self.right[2])
            with connect() as db:
                settings = failover.read_settings(db)
            self.assertIsNone(asyncio.run(failover.FailoverManager().sync(settings, SECRET)))
            self.assertEqual(server.seen, [])
        self.assertIn("not the one it was paired with", self.state()["last_sync_error"])
        with connect() as db:
            self.assertEqual(db.execute("SELECT state FROM incidents WHERE incident_key=?", (failover.CERTIFICATE_KEY,)).fetchone()[0], "active")


class ListenerTests(FailoverCase):
    """The link's own listener, as the web worker runs it: HTTPS with this NVR's certificate, the failover endpoints only, while failover is on."""

    def setUp(self):
        super().setUp()
        self.port = free_port()
        patch = mock.patch.dict(os.environ, {"NVR_FAILOVER_LINK_PORT": str(self.port), "NVR_FAILOVER_LINK_HOST": "127.0.0.1"})
        patch.start()
        self.addCleanup(patch.stop)
        failover._seen_nonces.clear()
        self.link = FailoverLink(check_seconds=0.1)
        self.addCleanup(self.link.stop)

    def renew(self, fingerprint: str) -> httpx.Response:
        async def go():
            headers = failover.sign(SECRET, "POST", LEASE, b"{}")
            async with failover.link_client({"peer_url": f"https://127.0.0.1:{self.port}", "peer_fingerprint": fingerprint}, failover.TIMEOUT) as client:
                response = await client.post(f"https://127.0.0.1:{self.port}{LEASE}", content=b"{}", headers=headers)
            response.extensions["failover_request_signature"] = headers["X-Failover-Signature"]
            return response
        return asyncio.run(go())

    def test_the_link_is_served_over_https_with_this_nvrs_certificate_and_nothing_else(self):
        self.link.start()
        time.sleep(1.5)
        self.assertIsNone(self.link.serving)  # Failover is off: no port is opened for it
        self.configure("standby")
        _, fingerprint = failover_link.certificate()
        eventually(lambda: self.link.serving, 10, "The link was not served")
        self.assertEqual(self.link.serving, ("127.0.0.1", self.port, fingerprint))
        answer = self.renew(fingerprint)
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertIsNone(failover.FailoverManager._answer_problem(SECRET, LEASE, answer))  # The standby's own signed answer
        self.assertTrue(answer.json()["granted"])
        # Nothing but the failover endpoints is there
        async def other(path):
            async with failover.link_client({"peer_url": "https://x", "peer_fingerprint": fingerprint}, failover.TIMEOUT) as client:
                return (await client.get(f"https://127.0.0.1:{self.port}{path}")).status_code
        for path in ("/healthz", "/api/admin/failover", "/api/failover/status", "/docs", "/openapi.json", "/"):
            self.assertEqual(asyncio.run(other(path)), 404, path)
        # The web port, unencrypted, is refused for the same request: this NVR does not allow an unencrypted link
        plain = TestClient(self.app).post(LEASE, content=b"{}", headers=failover.sign(SECRET, "POST", LEASE, b"{}"))
        self.assertEqual(plain.status_code, 403)

        # A new certificate: the listener serves it within moments, the old pin is refused, and the new one accepted
        renewed = self.admin.post("/api/admin/failover/link-certificate").json()["link"]["fingerprint"]
        new = failover_link.normalize_fingerprint(renewed)
        eventually(lambda: self.link.serving and self.link.serving[2] == new, 10, "The new certificate was not served")
        with self.assertRaises(failover_link.CertificateMismatch):
            self.renew(fingerprint)
        self.assertEqual(self.renew(new).status_code, 200)

        # Failover switched off: the port is closed again
        self.configure("off", peer_url=None)
        eventually(lambda: self.link.serving is None, 10, "The link was still served with failover off")
        with self.assertRaises(httpx.ConnectError):
            self.renew(new)

    def test_a_port_already_in_use_is_reported_and_the_rest_goes_on(self):
        blocker = socket.create_server(("127.0.0.1", self.port))
        self.addCleanup(blocker.close)
        self.configure("standby")
        collected = Collected()
        logging.getLogger("nvr.failover").addHandler(collected)
        self.addCleanup(logging.getLogger("nvr.failover").removeHandler, collected)
        self.link.start()
        eventually(lambda: any("could not listen" in message for message in collected.messages), 10, "The busy port was not reported")
        self.assertIsNone(self.link.serving)
        self.assertEqual(sum("could not listen" in message for message in collected.messages), 1)  # Said once, not every few seconds
        blocker.close()
        eventually(lambda: self.link.serving, 10, "The link did not start once the port was free")


class UnencryptedTests(FailoverCase):
    def setUp(self):
        super().setUp()
        failover._seen_nonces.clear()

    def plain(self):
        return TestClient(self.app).post(LEASE, content=b"{}", headers=failover.sign(SECRET, "POST", LEASE, b"{}"))

    def test_this_nvr_does_not_send_over_plain_http_unless_it_allows_it(self):
        self.configure("primary", peer_url="http://192.0.2.20:8280", allow_unencrypted=True)
        with connect() as db:
            db.execute("UPDATE failover_settings SET allow_unencrypted=0")  # As though the box were not ticked here
            settings = failover.read_settings(db)
        with self.assertRaises(failover.LinkRefused):
            failover.link_client(settings, failover.TIMEOUT)
        asyncio.run(failover.FailoverManager()._renew(settings, SECRET))
        self.assertEqual(self.state()["peer_state"], "refused")
        self.assertIn("Allow an unencrypted link", self.state()["last_error"])
        failover.link_client({**settings, "allow_unencrypted": True}, failover.TIMEOUT)  # Ticked: allowed

    def test_this_nvr_does_not_answer_a_request_that_arrived_unencrypted_unless_it_allows_it(self):
        self.configure("standby")
        with connect() as db:
            failover._set_state(db, last_renewed_at=None)
        refused = self.plain()
        self.assertEqual(refused.status_code, 403)
        self.assertIn("does not accept the failover link unencrypted", refused.json()["detail"])
        # A primary signed right is still heard as alive, so a standby does not take over from it while the two are moved to HTTPS
        self.assertIsNotNone(self.state()["last_renewed_at"])
        self.assertIn("unencrypted", self.state()["last_error"])
        self.assertIsNone(failover.check_takeover(datetime.now(timezone.utc) + timedelta(seconds=5)))
        # The same request over HTTPS (the link, or a proxy this NVR trusts) is answered
        self.assertEqual(TestClient(self.app, base_url=LINK).post(LEASE, content=b"{}", headers=failover.sign(SECRET, "POST", LEASE, b"{}")).status_code, 200)
        # Ticked on this NVR: answered unencrypted too
        self.configure("standby", peer_url="http://192.0.2.10:8280", allow_unencrypted=True)
        self.assertEqual(self.plain().status_code, 200)

    def test_both_must_allow_it(self):
        """A primary that allows an unencrypted link, and a standby that does not: the standby refuses, and the primary says why."""
        self.configure("standby")
        standby = self.app

        def answer(request: httpx.Request) -> httpx.Response:
            replied = TestClient(standby).request(request.method, request.url.raw_path.decode(), content=request.content, headers=dict(request.headers))
            return httpx.Response(replied.status_code, content=replied.content, headers=dict(replied.headers))

        real = httpx.AsyncClient
        with connect() as db:
            settings = {**failover.read_settings(db), "role": "primary", "peer_url": "http://192.0.2.10:8280", "allow_unencrypted": True}
        recorded: list = []
        with mock.patch.object(failover.httpx, "AsyncClient", lambda **options: real(transport=httpx.MockTransport(answer), **options)), \
                mock.patch.object(failover.FailoverManager, "_record_peer", staticmethod(lambda state, error: recorded.append((state, error)))):
            asyncio.run(failover.FailoverManager()._renew(settings, SECRET))
        self.assertEqual(recorded[0][0], "refused")
        self.assertIn("does not accept the failover link unencrypted", recorded[0][1])

    def test_the_hardening_page_warns_while_an_unencrypted_link_is_allowed(self):
        self.assertIsNone(self.admin.get("/api/hardening").json()["failover_link"])
        self.configure("primary", peer_url="https://192.0.2.20:8443", peer_fingerprint="ab" * 32)
        self.assertIsNone(self.admin.get("/api/hardening").json()["failover_link"])  # Pinned HTTPS: nothing to warn of
        self.configure("primary", peer_url="http://192.0.2.20:8280", allow_unencrypted=True)
        warning = self.admin.get("/api/hardening").json()["failover_link"]
        self.assertEqual((warning["sends_unencrypted"], warning["accepts_unencrypted"], warning["permission"]), (True, True, "allowed"))


class GrandfatheredTests(FailoverCase):
    """A pair on plain HTTP from before the upgrade keeps working as it was, until its address is changed, and is listed on the hardening page."""

    OLD = "http://192.0.2.20:8280"

    def setUp(self):
        super().setUp()
        failover._seen_nonces.clear()
        self.configure("standby", peer_url=self.OLD, allow_unencrypted=True)
        with connect() as db:  # What the upgrade leaves: the pair as it was, kept, with no box ticked
            db.execute("UPDATE failover_settings SET allow_unencrypted=0,legacy_link=1")

    def test_it_keeps_working_both_ways(self):
        with connect() as db:
            settings = failover.read_settings(db)
        failover.link_client(settings, failover.TIMEOUT)  # Sends to the plain address as before
        self.assertEqual(TestClient(self.app).post(LEASE, content=b"{}", headers=failover.sign(SECRET, "POST", LEASE, b"{}")).status_code, 200)  # And answers it

    def test_it_is_listed_on_the_hardening_page(self):
        warning = self.admin.get("/api/hardening").json()["failover_link"]
        self.assertEqual(warning, {"peer_url": self.OLD, "sends_unencrypted": True, "accepts_unencrypted": True, "unpinned": False,
                                   "permission": "kept_from_upgrade"})

    def test_saved_again_unchanged_it_is_kept_and_a_new_address_follows_the_new_rules(self):
        self.configure("standby", peer_url=self.OLD, lease_seconds=6)  # Another setting changed, the address not
        with connect() as db:
            self.assertTrue(failover.read_settings(db)["legacy_link"])
        answer = self.admin.put("/api/admin/failover", json={"role": "standby", "peer_url": "http://192.0.2.21:8280", "silence_seconds": 12, "lease_seconds": 5, "sync_seconds": 60})
        self.assertEqual(answer.status_code, 422)
        # Moved to HTTPS: no longer kept, and no longer listed
        self.configure("standby", peer_url="https://192.0.2.20:8443", peer_fingerprint="cd" * 32)
        with connect() as db:
            self.assertFalse(failover.read_settings(db)["legacy_link"])
        self.assertIsNone(self.admin.get("/api/hardening").json()["failover_link"])
        self.assertEqual(TestClient(self.app).post(LEASE, content=b"{}", headers=failover.sign(SECRET, "POST", LEASE, b"{}")).status_code, 403)


class UpgradeTests(unittest.TestCase):
    """The migration keeps every pair configured before it, and nothing else."""

    def run_upgrade(self, role: str, peer_url: str | None) -> dict:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"NVR_DB_PATH": str(Path(directory) / "state.sqlite3"), "NVR_RECORDINGS_PATH": directory,
                                                                                      "NVR_CREDENTIAL_KEY_PATH": str(Path(directory) / "credentials.key")}):
            before = [(version, module) for version, module in migrations.MIGRATIONS if version < 110]
            with mock.patch.object(migrations, "MIGRATIONS", before):
                initialize()
            with connect() as db:
                db.execute("UPDATE failover_settings SET role=?,peer_url=?", (role, peer_url))
            self.assertIn(110, migrations.migrate(database_path(), connect))
            with connect() as db:
                settings = failover.read_settings(db)
                settings["hardening"] = hardening.failover_link_warning(db)
            return settings

    def test_an_existing_pair_is_kept_and_listed(self):
        kept = self.run_upgrade("primary", "http://192.0.2.20:8280")
        self.assertEqual((kept["legacy_link"], kept["allow_unencrypted"], kept["peer_fingerprint"]), (True, False, None))
        self.assertEqual(kept["hardening"]["permission"], "kept_from_upgrade")
        failover.link_client(kept, failover.TIMEOUT)
        through_proxy = self.run_upgrade("standby", "https://nvr-primary.example.test")  # One through a proxy with a public certificate: kept too
        self.assertTrue(through_proxy["legacy_link"])
        self.assertEqual((through_proxy["hardening"]["sends_unencrypted"], through_proxy["hardening"]["accepts_unencrypted"]), (False, True))

    def test_an_nvr_not_in_a_pair_is_not(self):
        self.assertFalse(self.run_upgrade("off", None)["legacy_link"])


class HttpsClusterTests(unittest.TestCase):
    """Two real NVRs, as separate processes, paired over their HTTPS links: each pins the other's certificate, the primary renews its lease,
    the standby copies the configuration, and a wrong pin is refused."""

    APP = "tests.fixtures.failover_app:app"

    @classmethod
    def setUpClass(cls):
        env = {"NVR_BACKUP_PASSPHRASE": PASSPHRASE, "NVR_TEST_FAILOVER_TICK": "0.5", "NVR_FAILOVER_LINK_HOST": "127.0.0.1"}
        cls.primary = NvrSite("primary", app=cls.APP, env={**env, "NVR_FAILOVER_LINK_PORT": str(free_port())})
        cls.primary.env["NVR_BACKUP_PATH"] = str(cls.primary.base / "backups")
        cls.primary.start()
        cls.primary.seed(["Gate A1"], group="Gates")
        archive = subprocess.run([sys.executable, "-m", "app.backup", "create"], cwd=ROOT, env=cls.primary.env, capture_output=True, text=True, timeout=60)
        assert archive.returncode == 0, archive.stderr
        cls.standby = NvrSite("standby", app=cls.APP, env={**env, "NVR_FAILOVER_LINK_PORT": str(free_port())})
        cls.standby.env["NVR_BACKUP_PATH"] = str(cls.standby.base / "backups")
        restored = subprocess.run([sys.executable, "-m", "app.backup", "restore", str(max((cls.primary.base / "backups").glob("*.nvrbackup"))), "--force", "--standby"],
                                  cwd=ROOT, env=cls.standby.env, capture_output=True, text=True, timeout=60)
        assert restored.returncode == 0, restored.stderr
        cls.standby.start()
        cls.standby_client = httpx.Client(base_url=cls.standby.url, timeout=10)
        assert cls.standby_client.post("/api/auth/login", json={"username": "primary-admin", "password": PASSWORD}).status_code == 200

    @classmethod
    def tearDownClass(cls):
        cls.standby_client.close()
        cls.standby.stop()
        cls.primary.stop()

    def client(self, node: NvrSite) -> httpx.Client:
        return node.admin() if node is self.primary else self.standby_client

    def link(self, node: NvrSite) -> str:
        return f"https://127.0.0.1:{node.env['NVR_FAILOVER_LINK_PORT']}"

    def pair(self, node: NvrSite, role: str, peer: NvrSite, fingerprint: str | None = None) -> None:
        fingerprint = fingerprint or self.client(peer).get("/api/admin/failover").json()["link"]["fingerprint"]
        response = self.client(node).put("/api/admin/failover", json={"role": role, "peer_url": self.link(peer), "peer_fingerprint": fingerprint, "secret": SECRET,
                                                                      "silence_seconds": 60, "lease_seconds": 5, "sync_seconds": 600, "allow_private": True})
        self.assertEqual(response.status_code, 200, response.text)

    def status(self, node: NvrSite) -> dict:
        return self.client(node).get("/api/failover/status").json()

    @staticmethod
    def incident(node: NvrSite) -> str | None:
        db = sqlite3.connect(node.env["NVR_DB_PATH"], timeout=10)
        try:
            row = db.execute("SELECT state FROM incidents WHERE incident_key=?", (failover.CERTIFICATE_KEY,)).fetchone()
            return row[0] if row else None
        finally:
            db.close()

    def test_a_pair_over_https_renews_copies_and_refuses_a_wrong_certificate(self):
        primary_fingerprint = self.client(self.primary).get("/api/admin/failover").json()["link"]["fingerprint"]
        standby_fingerprint = self.client(self.standby).get("/api/admin/failover").json()["link"]["fingerprint"]
        self.assertNotEqual(primary_fingerprint, standby_fingerprint)  # The standby, restored from the primary's backup, has a certificate of its own
        self.pair(self.standby, "standby", self.primary)
        self.pair(self.primary, "primary", self.standby)
        eventually(lambda: self.status(self.primary)["peer_state"] == "ok", 30, f"The primary was never granted a lease over HTTPS: {self.status(self.primary)}")
        self.assertEqual(self.status(self.primary)["link"], "pinned")
        self.assertEqual(self.client(self.standby).post("/api/admin/failover/sync").status_code, 202)
        eventually(lambda: self.status(self.standby)["last_sync_at"], 30, f"The standby did not copy the configuration over HTTPS: {self.status(self.standby)}")
        self.assertIsNone(self.status(self.standby)["last_sync_error"])
        # Nothing went over the web ports: neither allows an unencrypted link, and both are still paired
        self.assertEqual(httpx.post(f"{self.primary.url}{LEASE}", content=b"{}", headers=failover.sign(SECRET, "POST", LEASE, b"{}")).status_code, 403)

        # The primary pinned to a certificate the standby does not present: refused, said, and an incident raised
        self.pair(self.primary, "primary", self.standby, fingerprint="ab" * 32)
        eventually(lambda: self.status(self.primary)["peer_state"] == "wrong_certificate", 30, "A wrong certificate was not refused")
        self.assertIn("not the one it was paired with", self.status(self.primary)["last_error"])
        self.assertEqual(self.incident(self.primary), "active")
        # Paired again with the right one: it recovers
        self.pair(self.primary, "primary", self.standby, fingerprint=standby_fingerprint)
        eventually(lambda: self.status(self.primary)["peer_state"] == "ok", 30, "The pair did not recover once paired again")
        self.assertEqual(self.incident(self.primary), "recovered")


if __name__ == "__main__":
    unittest.main()
