"""Wrong passwords for one account from outside the local network are capped at a fixed number per hour (#457, the owner's decision).

The account-wide slowdown forgets failures after a quiet spell, so a guesser spreading tries over many addresses, in bursts of 19 with a
pause between, used to get about 76 an hour without limit. At ``ACCOUNT_HOUR_CAP`` the account refuses attempts from outside, a right
password included, until the clock hour ends; the local network and browsers that signed in to it before are not counted and never refused."""
from __future__ import annotations

from datetime import timedelta

from app import auth_limits as limits
from app.store import connect
from tests.test_auth_limit_spared import ACCOUNT, SparedCase, caller

BURST = limits.LIMITS["account"][0]  # As many as the account-wide slowdown lets through before it starts waiting


class AccountHourCapTests(SparedCase):
    def outsider(self) -> limits.Caller:
        self.addresses = getattr(self, "addresses", 0) + 1
        return caller(f"198.51.{100 + self.addresses // 250}.{self.addresses % 250 + 1}")

    def burst(self, count: int = BURST) -> int:
        """``count`` wrong passwords from new outside addresses; how many were let through to the password check."""
        return sum(self.attempt(self.outsider()) for _ in range(count))

    def quiet(self) -> None:
        self.now += limits.FORGET_AFTER + timedelta(minutes=1)  # The account-wide slowdown forgets: only the hourly cap remembers

    def test_bursts_of_guesses_from_many_addresses_stop_at_the_cap_until_the_hour_ends(self):
        let_through = 0
        for _ in range(3):  # 09:00, 09:16, 09:32: three bursts the slowdown alone lets through
            let_through += self.burst()
            self.quiet()
        self.assertEqual(let_through, 3 * BURST)
        self.assertEqual(self.burst(limits.ACCOUNT_HOUR_CAP - 3 * BURST), limits.ACCOUNT_HOUR_CAP - 3 * BURST)  # Up to the cap: let through
        self.assertFalse(self.attempt(self.outsider()))  # The next, from a new address: refused
        self.now = self.now.replace(minute=59)
        self.assertFalse(self.attempt(self.outsider()))  # Still the same hour, however quiet it has been
        # The local network and a known browser are not held by the cap
        self.assertTrue(self.attempt(caller("192.168.1.20", local=True)))
        self.now = self.now.replace(minute=0) + timedelta(hours=1)
        self.assertTrue(self.attempt(self.outsider()))  # A new hour

    def test_below_the_cap_nothing_is_refused_by_it(self):
        for _ in range(3):
            self.assertEqual(self.burst(), BURST)
            self.quiet()
        with connect() as db:
            self.assertEqual(db.execute("SELECT failures FROM auth_account_hours WHERE account=?", (ACCOUNT,)).fetchone()[0], 3 * BURST)
        self.assertTrue(self.attempt(self.outsider()))

    def test_local_network_failures_do_not_count_towards_it(self):
        for _ in range(limits.ACCOUNT_HOUR_CAP + 5):
            self.attempt(caller("192.168.1.20", local=True))  # Slowed by its own address limit, never counted against the account's hour
        with connect() as db:
            self.assertIsNone(db.execute("SELECT failures FROM auth_account_hours WHERE account=?", (ACCOUNT,)).fetchone())
        self.assertTrue(self.attempt(self.outsider()))

    def test_a_right_password_is_taken_back_and_a_completed_sign_in_clears_the_hour(self):
        self.burst(10)
        right = self.outsider()
        limits.begin(ACCOUNT, right)
        limits.release(ACCOUNT, right)  # The password was right: not a guess
        with connect() as db:
            self.assertEqual(db.execute("SELECT failures FROM auth_account_hours WHERE account=?", (ACCOUNT,)).fetchone()[0], 10)
        limits.succeeded(ACCOUNT, right)
        with connect() as db:
            self.assertIsNone(db.execute("SELECT failures FROM auth_account_hours WHERE account=?", (ACCOUNT,)).fetchone())

    def test_reaching_the_cap_is_logged_once_and_raises_the_guessing_incident(self):
        for _ in range(3):
            self.burst()
            self.quiet()
        self.burst(limits.ACCOUNT_HOUR_CAP - 3 * BURST)
        self.attempt(self.outsider())
        with connect() as db:
            capped = db.execute("SELECT COUNT(*) FROM audit_log WHERE action='auth_throttled' AND details_json LIKE '%hourly_cap%'").fetchone()[0]
            incident = db.execute("SELECT fault_active FROM incidents WHERE incident_key=?", (f"signin_guessing:{ACCOUNT}",)).fetchone()
        self.assertEqual(capped, 1)
        self.assertEqual(incident[0], 1)

    def test_only_this_hour_and_the_last_are_kept(self):
        self.burst(5)
        self.now += timedelta(hours=3)
        self.burst(5)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM auth_account_hours").fetchone()[0], 1)


del SparedCase  # Its own tests run from their own module
