"""Sign-in cannot use every thread for password hashing, and an IPv6 network is one caller for pending passkey sign-ins (#462)."""
from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth_limits as limits
from app import security
from app.api import identity
from app.api import mfa as mfa_api
from app.store import connect
from tests import test_bootstrap
from tests.test_support import TEST_PASSWORD, setup_code


class SignInLoadTests(unittest.TestCase):
    setUp = test_bootstrap.BootstrapTests.setUp
    tearDown = test_bootstrap.BootstrapTests.tearDown

    def administrator(self) -> None:
        created = self.client.post("/api/setup/admin", json={"username": "keeper", "password": TEST_PASSWORD, "setup_code": setup_code()})
        self.assertEqual(created.status_code, 201, created.text)
        self.client.post("/api/auth/logout")

    def test_only_a_few_password_checks_run_at_once_and_the_rest_wait_their_turn(self):
        self.administrator()
        self.assertTrue(2 <= security.SIGN_IN_CHECKS_AT_ONCE <= 8)
        guard, running, most = threading.Lock(), [0], [0]
        real = identity.verify_password

        def watched(password, encoded):
            with guard:
                running[0] += 1
                most[0] = max(most[0], running[0])
            try:
                time.sleep(0.05)
                return real(password, encoded)
            finally:
                with guard:
                    running[0] -= 1

        def attempt(number: int) -> int:
            # Twenty-four at once, each a different name so no limit on guesses is met. A client of its own each, without the application's
            # start-up and shutdown (entered once, by setUp's client)
            return TestClient(self.client.app).post("/api/auth/login", json={"username": f"nobody-{number}", "password": "wrong-password-123"}).status_code

        with patch.object(identity, "verify_password", watched), patch.object(security, "verify_password", watched), \
                patch.object(security, "SIGN_IN_CHECK_WAIT_SECONDS", 30.0), patch.object(limits, "LIMITS", {scope: (10_000, 2) for scope in limits.LIMITS}):
            with ThreadPoolExecutor(24) as pool:
                answers = list(pool.map(attempt, range(24)))
        self.assertEqual(set(answers), {401})  # Every one was checked in the end
        self.assertLessEqual(most[0], security.SIGN_IN_CHECKS_AT_ONCE)
        self.assertGreaterEqual(most[0], 2)

    def test_a_sign_in_that_finds_no_place_is_told_to_try_again_and_is_not_counted_as_a_wrong_password(self):
        self.administrator()
        held = [security._sign_in_checks.acquire(timeout=1) for _ in range(security.SIGN_IN_CHECKS_AT_ONCE)]
        self.addCleanup(lambda: [security._sign_in_checks.release() for taken in held if taken])
        with patch.object(security, "SIGN_IN_CHECK_WAIT_SECONDS", 0.1):
            busy = self.client.post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD})
        self.assertEqual((busy.status_code, busy.headers.get("retry-after")), (429, "2"))
        self.assertIn("Try again in a few seconds", busy.json()["detail"])
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_failures").fetchone()[0], 0)
        for taken in held:
            security._sign_in_checks.release()
        held.clear()
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "keeper", "password": TEST_PASSWORD}).status_code, 200)

    def test_addresses_in_one_ipv6_64_share_the_cap_on_pending_passkey_sign_ins(self):
        mfa_api._passkey_pending.clear()
        self.addCleanup(mfa_api._passkey_pending.clear)
        for number in range(1, 6):
            mfa_api._passkey_pending[f"challenge-{number}"] = (f"2001:db8:1:2::{number:x}", time.monotonic())
        mfa_api._passkey_pending["other-network"] = ("2001:db8:1:3::1", time.monotonic())
        mfa_api._passkey_pending["v4"] = ("198.51.100.7", time.monotonic())
        self.assertEqual(mfa_api._waiting_from("2001:db8:1:2:ffff::99"), 5)  # A sixth address of the same host: it is the same caller
        self.assertEqual(mfa_api._waiting_from("2001:db8:1:3::2"), 1)
        self.assertEqual((mfa_api._waiting_from("198.51.100.7"), mfa_api._waiting_from("198.51.100.8")), (1, 0))  # IPv4 as before: each address its own


if __name__ == "__main__":
    unittest.main()
