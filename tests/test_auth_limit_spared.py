"""A handful of outside addresses cannot keep an account's owner from signing in (#457, the decision on the ticket).

The account-wide wait still slows everyone from outside, a right password included: that is what bounds guessing spread over many
addresses. It leaves alone a caller on the local network and a browser that has signed in to the account before."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import auth_limits as limits
from app import known_browser
from app.security import hash_password
from app.store import connect, initialize
from tests import test_remote_access
from tests.test_remote_access import OPERATOR_PASSWORD
from tests.test_support import TEST_PASSWORD

ACCOUNT = "admin"
ATTACKERS = [f"198.51.100.{number}" for number in range(1, 9)]
NINTH = "203.0.113.77"


def caller(address: str, *, local: bool = False, tokens: tuple[str, ...] = ()) -> limits.Caller:
    made = limits.Caller(address)
    made.local, made.tokens = local, tokens
    return made


class SparedCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        environment = patch.dict(os.environ, {"NVR_DB_PATH": str(root / "state.sqlite3"), "NVR_CREDENTIAL_KEY_PATH": str(root / "credentials.key")})
        environment.start()
        self.addCleanup(environment.stop)
        initialize()
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,'administrator')", (ACCOUNT, hash_password(TEST_PASSWORD)))
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('other',?,'operator')", (hash_password(OPERATOR_PASSWORD),))
            self.user_id = db.execute("SELECT id FROM users WHERE username=?", (ACCOUNT,)).fetchone()[0]
        self.now = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
        clock = patch.object(limits, "_now", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def attempt(self, address: str, account: str = ACCOUNT) -> bool:
        """One attempt that reaches the password check (and is left counted, as a wrong password is); False when it was made to wait."""
        try:
            limits.begin(account, address)
            return True
        except HTTPException:
            return False

    def token(self, user_id: int | None = None) -> tuple[str, ...]:
        """The cookie a browser holds after a sign-in to the account was completed in it."""
        from fastapi import Response

        response = Response()
        with connect() as db:
            known_browser.remember(db, (), response, self.user_id if user_id is None else user_id, secure=False)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        return tuple(cookie.split(";")[0].split("=", 1)[1].split("~"))

    def an_hour_of_guessing(self) -> int:
        """Eight outside addresses guess for an hour the way that keeps an account waiting: each sends four wrong passwords every sixteen
        minutes, so none is slowed on its own account and every guess is added to the account's count, and each guess is sent the moment
        the account-wide wait lets one through. Ends just after a guess has landed. Returns how many reached the password check."""
        reached, sent = 0, {}
        for second in range(3600):
            turn = second // 960
            address = next((address for address in ATTACKERS if sent.get((turn, address), 0) < 4), None)
            if address is not None and self.attempt(address):
                sent[(turn, address)] = sent.get((turn, address), 0) + 1
                reached += 1
            self.now += timedelta(seconds=1)
        while not self.attempt(ATTACKERS[-1]):
            self.now += timedelta(seconds=1)
        return reached + 1

    def hammering(self) -> int:
        """The same eight addresses each sending a wrong password every five seconds for an hour. Returns how many reached the check."""
        reached = 0
        for _ in range(3600 // 5):
            for address in ATTACKERS:
                reached += self.attempt(address)
            self.now += timedelta(seconds=5)
        return reached

    def incident(self) -> str | None:
        with connect() as db:
            row = db.execute("SELECT fault_active FROM incidents WHERE incident_key=?", (f"signin_guessing:{ACCOUNT}",)).fetchone()
        return None if row is None else ("open" if row["fault_active"] else "recovered")


class OwnerGetsInTests(SparedCase):
    def test_after_an_hour_of_guessing_from_eight_addresses_the_owner_signs_in_from_the_site_or_from_their_own_browser(self):
        token = self.token()
        self.an_hour_of_guessing()
        self.assertTrue(self.attempt(ATTACKERS[0]) is False, "the account was not waiting")
        # From any other outside address a right password still waits: that is what bounds guessing spread over many addresses
        self.assertFalse(self.attempt(caller(NINTH)))
        # On the local network: straight in, no wait
        self.assertTrue(self.attempt(caller("192.0.2.20", local=True)))
        # From outside, in a browser that has signed in to this account before: straight in, no wait
        self.assertTrue(self.attempt(caller(NINTH, tokens=token)))
        # A browser that signed in to another account is not spared for this one, and a made-up token spares nobody
        with connect() as db:
            other = db.execute("SELECT id FROM users WHERE username='other'").fetchone()[0]
        self.assertFalse(self.attempt(caller("203.0.113.78", tokens=self.token(other))))
        self.assertFalse(self.attempt(caller("203.0.113.79", tokens=(f"{self.user_id}.{int(time.time())}.{'0' * 64}",))))

    def test_guessing_stays_bounded_and_those_spared_add_nothing_to_the_accounts_count(self):
        reached = self.an_hour_of_guessing()
        self.assertLessEqual(reached, 45, "more than 45 guesses in the hour reached the password check")  # Sent one a second, whenever let
        self.assertGreater(reached, 19)
        with connect() as db:
            before = db.execute("SELECT failures FROM auth_failures WHERE scope='account' AND key=?", (ACCOUNT,)).fetchone()[0]
        # Someone on the local network who gets it wrong is slowed by their own count, and is not added to the count that slows outsiders
        local = caller("192.0.2.20", local=True)
        self.assertEqual([self.attempt(local) for _ in range(6)], [True, True, True, True, True, False])
        with connect() as db:
            self.assertEqual(db.execute("SELECT failures FROM auth_failures WHERE scope='account' AND key=?", (ACCOUNT,)).fetchone()[0], before)
        # The remembered browser too: its own five tries, then it waits like anyone
        remembered = caller(NINTH, tokens=self.token())
        self.assertEqual([self.attempt(remembered) for _ in range(6)], [True, True, True, True, True, False])

    def test_addresses_that_hammer_are_each_held_to_a_few_guesses_an_hour(self):
        # An address past its own allowance is slowed by its own count, up to five minutes a guess, and (since #365) adds nothing more to
        # the account's count. So the most that reach the password check grows with the number of addresses: at most 18 an hour for each.
        self.assertLessEqual(self.hammering(), 18 * len(ATTACKERS))  # Against 5,760 sent

    def test_an_incident_says_someone_is_guessing_and_recovers_when_the_account_is_signed_in_to(self):
        self.assertIsNone(self.incident())
        self.an_hour_of_guessing()
        self.assertEqual(self.incident(), "open")
        with connect() as db:
            summary = db.execute("SELECT summary FROM incidents WHERE incident_key=?", (f"signin_guessing:{ACCOUNT}",)).fetchone()[0]
        self.assertIn(ACCOUNT, summary)
        owner = caller("192.0.2.20", local=True)
        limits.begin(ACCOUNT, owner)
        limits.succeeded(ACCOUNT, owner)
        self.assertNotEqual(self.incident(), "open")

    def test_the_incident_recovers_by_itself_once_the_guessing_has_been_quiet(self):
        self.an_hour_of_guessing()
        self.assertEqual(self.incident(), "open")
        self.now += timedelta(minutes=30)
        self.attempt(caller("192.0.2.33", local=True), account="other")  # Any sign-in attempt at all, to any account
        self.assertNotEqual(self.incident(), "open")

    def test_a_name_that_is_no_account_raises_no_incident(self):
        for address in ATTACKERS:
            for _ in range(4):
                self.attempt(address, account="not-an-account")
        self.assertFalse(self.attempt(NINTH, account="not-an-account"))  # The account-wide wait has started for that name
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM incidents WHERE fault_type='signin_guessing'").fetchone()[0], 0)


class RememberedBrowserTests(SparedCase):
    def knows(self, tokens: tuple[str, ...], account: str = ACCOUNT) -> bool:
        with connect() as db:
            return known_browser.knows(db, tokens, account)

    def test_the_cookie_ends_when_the_password_changes_or_the_account_is_disabled_and_after_its_time(self):
        token = self.token()
        self.assertTrue(self.knows(token))
        self.assertTrue(self.knows(token, "ADMIN"))  # Names are matched as sign-in matches them
        self.assertFalse(self.knows(token, "other"))
        with patch.object(known_browser.time, "time", return_value=time.time() + (known_browser.DAYS + 1) * 86400):
            self.assertFalse(self.knows(token))
        with connect() as db:
            db.execute("UPDATE users SET disabled_at='2026-11-03T09:00:00.000Z' WHERE id=?", (self.user_id,))
        self.assertFalse(self.knows(token))
        with connect() as db:
            db.execute("UPDATE users SET disabled_at=NULL, password_hash=? WHERE id=?", (hash_password(OPERATOR_PASSWORD), self.user_id))
        self.assertFalse(self.knows(token))  # The password changed: the browser is a stranger again until it signs in

    def test_a_browser_holds_a_cookie_for_each_of_a_few_accounts(self):
        from fastapi import Response

        with connect() as db:
            other = db.execute("SELECT id FROM users WHERE username='other'").fetchone()[0]
            response = Response()
            known_browser.remember(db, self.token(), response, other, secure=True)
        cookie = response.headers["set-cookie"]
        self.assertIn("Secure", cookie)
        both = tuple(cookie.split(";")[0].split("=", 1)[1].split("~"))
        self.assertEqual(len(both), 2)
        self.assertTrue(self.knows(both) and self.knows(both, "other"))


class Ipv6Tests(SparedCase):
    def test_two_addresses_in_one_ipv6_64_share_a_bucket_and_another_64_does_not(self):
        self.assertEqual(limits.counted_as("2001:db8:1:2::1"), limits.counted_as("2001:db8:1:2:ffff:ffff:ffff:9"))
        self.assertNotEqual(limits.counted_as("2001:db8:1:2::1"), limits.counted_as("2001:db8:1:3::1"))
        self.assertEqual((limits.counted_as("198.51.100.7"), limits.counted_as("::ffff:198.51.100.7"), limits.counted_as("unknown")), ("198.51.100.7", "198.51.100.7", "unknown"))
        # One host walking through its /64 gets five tries in all, not five an address
        tries = [self.attempt(f"2001:db8:1:2::{number:x}") for number in range(1, 9)]
        self.assertEqual(tries, [True] * 5 + [False] * 3)
        self.assertTrue(self.attempt("2001:db8:1:3::1"))


class ThroughTheApiTests(test_remote_access.ProxiedCase):
    """Sign-in, and the password confirmation asked of a signed-in person, follow the rule: both reach the limits through the same door."""

    def setUp(self):
        super().setUp()
        self.trust("127.0.0.1")
        self.add_operator()

    filled = 0

    def fill_the_account(self) -> None:
        """Wrong passwords for the account from seven addresses that have not guessed before (one that is already being slowed adds
        nothing to the account's count), enough to start the account-wide wait."""
        for _ in range(7):
            self.filled += 1
            for _ in range(4):
                limits.record_failure("viewer", f"198.51.100.{self.filled}")

    def sign_in(self, client):
        return client.post("/api/auth/login", json={"username": "viewer", "password": OPERATOR_PASSWORD})

    def test_a_right_password_from_outside_waits_unless_the_browser_has_signed_in_before_or_is_on_site(self):
        self.proxy.client = "203.0.113.9"
        returning, stranger = self.through_proxy(), self.through_proxy()
        first = self.sign_in(returning)
        self.assertEqual(first.status_code, 200, first.text)
        cookie = [header for header in first.headers.get_list("set-cookie") if header.startswith(known_browser.COOKIE)][0].lower()
        self.assertTrue("httponly" in cookie and "samesite=strict" in cookie, cookie)
        self.fill_the_account()

        self.proxy.client = "203.0.113.200"  # Both from an address that has never failed
        self.assertEqual(self.sign_in(stranger).status_code, 429)
        self.assertEqual(self.sign_in(returning).status_code, 200)
        self.fill_the_account()  # A completed sign-in clears the count: fill it again
        on_site = self.local_client()
        self.assertEqual(self.sign_in(on_site).status_code, 200)

        # Password confirmation (changing the password, here) follows the same rule, and the change ends the old cookie
        self.fill_the_account()
        changed = returning.post("/api/me/security/password", json={"current_password": OPERATOR_PASSWORD, "new_password": TEST_PASSWORD + "-new"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.fill_the_account()
        again = returning.post("/api/auth/login", json={"username": "viewer", "password": TEST_PASSWORD + "-new"})
        self.assertEqual(again.status_code, 429)  # The cookie was made for the old password: this browser waits like any other until it signs in

    def test_every_step_that_counts_attempts_takes_its_caller_from_the_one_place(self):
        root = Path(__file__).resolve().parents[1] / "app" / "api"
        for name in ("identity.py", "mfa.py"):
            source = (root / name).read_text(encoding="utf-8")
            self.assertTrue("limits.client_address(request)" in source, name)
            for line in source.splitlines():
                if "limits.begin" in line or "limits.record_failure" in line or "limits.release" in line:
                    self.assertTrue("address" in line and "network.client_address" not in line, line.strip())


if __name__ == "__main__":
    unittest.main()
