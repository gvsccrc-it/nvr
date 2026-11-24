"""Sign-ins, failed sign-ins, Entra accounts, and changes to retention, schedules and groups are in the security log (#459).

By the decision on the ticket: every completed sign-in (password, passkey, Entra, first-run setup, video wall) with its method, the address
and whether it counted as local; a failed sign-in for an account that exists (the first of a run, and the throttle thresholds), at most ten
rows per account an hour; an Entra account being created and its role changing; retention, schedule, group membership and group deletion
with the value before and after. No row holds a password, token or code."""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import auth_limits, mfa, security_log
from app.audit import record
from app.store import connect
from tests.test_second_factors import OPERATOR_PASSWORD, SecondFactorCase
from tests.test_support import TEST_OIDC_CLIENT_SECRET, TEST_OIDC_SESSION_SECRET, TEST_PASSWORD

TENANT = "11111111-1111-4111-8111-111111111111"


class LogCase(SecondFactorCase):
    def rows(self, action: str) -> list[dict]:
        return [{**entry, **entry["details"]} for entry in self.audit(action)]

    def assert_no_secret(self, *secrets: str) -> None:
        with connect() as db:
            text = " ".join(row[0] for row in db.execute("SELECT details_json FROM audit_log"))
        for secret in secrets:
            self.assertNotIn(secret, text)


class SignInTests(LogCase):
    def test_first_run_setup_and_a_password_sign_in_are_recorded_with_the_address_and_network(self):
        setup = self.rows("signed_in")
        # The test client's address is not on a local network by the NVR's rule, so it counts as from outside
        self.assertEqual([(row["method"], row["network"], row["actor_id"]) for row in setup], [("setup", "remote", 1)])
        self.assertTrue(setup[0]["address"])
        self.sign_in(self.browser())
        latest = self.rows("signed_in")[-1]
        self.assertEqual((latest["method"], latest["resource_type"], latest["resource_id"], latest["network"]), ("password", "user", "1", "remote"))
        self.assert_no_secret(TEST_PASSWORD)

    def test_a_sign_in_from_the_local_network_counts_as_local(self):
        with patch("app.network.request_is_remote", return_value=False):
            self.sign_in(self.browser())
        self.assertEqual(self.rows("signed_in")[-1]["network"], "local")

    def test_a_second_factor_sign_in_is_recorded_with_its_method_and_no_code(self):
        secret, codes = self.enrol_totp(self.admin)
        other = self.browser()
        challenge = self.sign_in(other)
        self.assertEqual([row["method"] for row in self.rows("signed_in")], ["setup"])  # The password alone is not a sign-in
        code = mfa.totp_code(secret, mfa.totp_step() + 1)
        self.assertEqual(other.post("/api/auth/mfa/verify", json={"challenge": challenge["challenge"], "method": "totp", "code": code}).status_code, 200)
        recovery = self.browser()
        started = self.sign_in(recovery)
        self.assertEqual(recovery.post("/api/auth/mfa/verify", json={"challenge": started["challenge"], "method": "recovery", "code": codes[0]}).status_code, 200)
        self.assertEqual([row["method"] for row in self.rows("signed_in")], ["setup", "password+totp", "password+recovery"])
        self.assert_no_secret(code, codes[0], TEST_PASSWORD)

    def test_a_video_wall_signing_in_is_recorded_by_its_token_name_never_the_token(self):
        made = self.admin.post("/api/admin/api-tokens", json={"name": "Lobby wall", "scopes": ["read", "media"]})
        self.assertEqual(made.status_code, 201, made.text)
        secret = made.json()["secret"]
        self.assertEqual(self.browser().post("/api/wall/session", json={"token": secret}).status_code, 200)
        [row] = self.rows("wall_signed_in")
        self.assertEqual((row["name"], row["resource_type"], row["resource_id"], row["network"]), ("Lobby wall", "api_token", str(made.json()["token"]["id"]), "remote"))
        self.assert_no_secret(secret)

    def test_an_entra_account_being_created_its_sign_ins_and_its_role_changing(self):
        os.environ.update({"NVR_PUBLIC_URL": "https://nvr.example.test", "NVR_OIDC_TENANT_ID": TENANT, "NVR_OIDC_CLIENT_ID": "client-id",
                           "NVR_OIDC_CLIENT_SECRET": TEST_OIDC_CLIENT_SECRET, "NVR_OIDC_SESSION_SECRET": TEST_OIDC_SESSION_SECRET,
                           "NVR_OIDC_OPERATOR_ROLES": "NVR-Operator", "NVR_OIDC_ADMIN_ROLES": "NVR-Admin"})
        self.addCleanup(lambda: [os.environ.pop(key, None) for key in ("NVR_OIDC_TENANT_ID", "NVR_OIDC_CLIENT_ID", "NVR_OIDC_CLIENT_SECRET",
                                                                          "NVR_OIDC_SESSION_SECRET", "NVR_OIDC_OPERATOR_ROLES", "NVR_OIDC_ADMIN_ROLES")])
        claims = {"sub": "object-123", "tid": TENANT, "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "preferred_username": "operator@example.test",
                  "roles": ["NVR-Operator"]}
        fake = type("FakeOIDC", (), {"authorize_access_token": AsyncMock(return_value={"userinfo": claims, "access_token": "secret-access-token"})})()

        def entra_sign_in():
            with patch("app.api.identity._oidc_client", return_value=fake):
                self.assertEqual(self.browser().get("/api/auth/oidc/callback?code=the-code&state=fake", follow_redirects=False).status_code, 303)
        entra_sign_in()
        [created] = self.rows("entra_account_created")
        self.assertEqual((created["username"], created["role"]), ("operator@example.test", "operator"))
        entra_sign_in()  # The same role: a sign-in, and no change
        self.assertEqual(self.rows("entra_role_changed"), [])
        claims["roles"] = ["NVR-Admin"]
        entra_sign_in()
        [changed] = self.rows("entra_role_changed")
        self.assertEqual((changed["before"], changed["after"], changed["resource_id"]), ("operator", "administrator", created["resource_id"]))
        self.assertEqual([row["method"] for row in self.rows("signed_in") if row["method"] == "entra"], ["entra"] * 3)
        self.assert_no_secret("secret-access-token", "the-code")


class FailedSignInTests(LogCase):
    def wrong(self, username: str = "operator", address: str | None = None) -> int:
        with patch.object(auth_limits.network, "client_address", return_value=address) if address else patch.object(auth_limits, "_now", auth_limits._now):
            return self.browser().post("/api/auth/login", json={"username": username, "password": "wrong password 12345"}).status_code

    def test_the_first_failure_for_an_account_is_recorded_and_the_next_in_the_run_is_not(self):
        user_id = self.add_operator()
        self.assertEqual(self.wrong(), 401)
        self.assertEqual(self.wrong(), 401)
        [row] = self.rows("sign_in_failed")
        self.assertEqual((row["resource_id"], row["method"], row["network"], row["actor_id"]), (str(user_id), "password", "remote", None))
        self.assert_no_secret("wrong password 12345", OPERATOR_PASSWORD)

    def test_an_unknown_username_is_never_written_as_typed(self):
        typed = "correct-horse-battery-staple"  # Someone's password, typed in the username box
        for _ in range(6):
            self.wrong(typed)
        self.assertEqual(self.rows("sign_in_failed"), [])
        throttled = self.rows("auth_throttled")
        self.assertTrue(throttled)
        self.assertTrue(all(row["resource_id"].startswith(("unknown:", "198.", "127.", "testclient")) or "unknown:" in row["resource_id"] for row in throttled))
        self.assert_no_secret(typed)

    def test_guessing_from_many_addresses_writes_at_most_ten_rows_for_the_account_in_an_hour(self):
        self.add_operator()
        # Five wrong passwords from each of twelve addresses on the local network: each address crosses its own threshold (a row each, thirteen
        # with the first failure). On the local network the account-wide wait leaves them alone, so every attempt is counted (#457).
        with patch("app.network.request_is_remote", return_value=False):
            for index in range(60):
                self.assertEqual(self.wrong(address=f"198.51.100.{index // 5 + 1}"), 401)
        with connect() as db:
            written = db.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('sign_in_failed','auth_throttled') AND "
                                 "(resource_id LIKE 'operator%' OR resource_type='user')").fetchone()[0]
        self.assertEqual(written, security_log.FAILED_SIGN_IN_ROWS_PER_HOUR)
        self.assertEqual(len(self.rows("sign_in_failed")), 1)

    def test_rows_older_than_an_hour_no_longer_count_against_the_bound(self):
        user_id = self.add_operator()
        with connect() as db:
            for _ in range(10):
                record(db, None, "auth_throttled", "pair", "operator|198.51.100.9", failures=5)
            self.assertFalse(security_log.failure_rows_left(db, "operator", user_id))
            db.execute("UPDATE audit_log SET created_at='2000-01-01T00:00:00.000Z'")
            self.assertTrue(security_log.failure_rows_left(db, "operator", user_id))
            # Another account's rows are its own
            record(db, None, "auth_throttled", "pair", "operator2|198.51.100.9", failures=5)
            self.assertTrue(security_log.failure_rows_left(db, "operator", user_id))


class SettingChangeTests(LogCase):
    def setUp(self):
        super().setUp()
        self.app.state.recorders = SimpleNamespace(sync_camera=AsyncMock())  # Schedules are picked up by the recorders, which these tests do not run
        self.group = self.admin.post("/api/camera-groups", json={"name": "Dock"}).json()["id"]
        self.cameras = [str(uuid.uuid4()) for _ in range(3)]
        with connect() as db:
            db.executemany("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')",
                           [(camera_id, f"Camera {index}") for index, camera_id in enumerate(self.cameras)])

    def test_retention_changes_hold_the_value_before_and_after(self):
        self.assertEqual(self.admin.put(f"/api/camera-groups/{self.group}/retention", json={"retention_days": 30}).status_code, 200)
        self.assertEqual(self.admin.put(f"/api/camera-groups/{self.group}/retention", json={"retention_days": 1}).status_code, 200)
        self.assertEqual(self.admin.put(f"/api/camera-groups/{self.group}/retention", json={"retention_days": 1}).status_code, 200)  # The same again
        self.assertEqual(self.admin.delete(f"/api/camera-groups/{self.group}/retention").status_code, 204)
        camera = self.cameras[0]
        self.assertEqual(self.admin.put(f"/api/cameras/{camera}/retention", json={"retention_days": 7}).status_code, 200)
        rows = self.rows("retention_changed")
        self.assertEqual([(row["resource_type"], (row["before"] or {}).get("retention_days"), (row["after"] or {}).get("retention_days")) for row in rows],
                         [("camera_group", None, 30), ("camera_group", 30, 1), ("camera_group", 1, None), ("camera", None, 7)])
        self.assertTrue(all(row["actor_id"] == 1 for row in rows))

    def test_schedule_changes_hold_the_value_before_and_after(self):
        week = [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "07:00", "end": "19:00", "mode": "continuous"}]
        self.assertEqual(self.admin.put(f"/api/camera-groups/{self.group}/schedule", json={"timezone": "UTC", "intervals": week}).status_code, 200, )
        self.assertEqual(self.admin.put(f"/api/camera-groups/{self.group}/schedule", json={"timezone": "UTC", "intervals": []}).status_code, 200)
        camera = self.cameras[0]
        self.assertEqual(self.admin.put(f"/api/cameras/{camera}/schedule", json={"timezone": "UTC", "intervals": week}).status_code, 200)
        self.assertEqual(self.admin.put(f"/api/cameras/{camera}/schedule", json={"inherit_group": True}).status_code, 200)
        rows = self.rows("schedule_changed")
        self.assertEqual([(row["resource_type"], row["before"] and len(row["before"]["intervals"]), row["after"] and len(row["after"]["intervals"])) for row in rows],
                         [("camera_group", None, 1), ("camera_group", 1, 0), ("camera", None, 1), ("camera", 1, None)])

    def test_membership_changes_and_a_deleted_group_are_recorded(self):
        first, second, third = sorted(self.cameras)
        self.assertEqual(self.admin.put(f"/api/camera-groups/{self.group}/members", json={"camera_ids": [first, second]}).status_code, 200)
        self.assertEqual(self.admin.put(f"/api/camera-groups/{self.group}/members", json={"camera_ids": [second, first]}).status_code, 200)  # The same
        self.assertEqual(self.admin.put(f"/api/camera-groups/{self.group}/members", json={"camera_ids": [second, third]}).status_code, 200)
        rows = self.rows("camera_group_members_changed")
        self.assertEqual([(row["before"], row["after"], row["added"], row["removed"]) for row in rows],
                         [([], [first, second], [first, second], []), ([first, second], [second, third], [third], [first])])
        self.admin.put(f"/api/camera-groups/{self.group}/retention", json={"retention_days": 1})
        self.assertEqual(self.admin.delete(f"/api/camera-groups/{self.group}").status_code, 204)
        [deleted] = self.rows("camera_group_deleted")
        self.assertEqual((deleted["name"], deleted["members"], deleted["retention"]["retention_days"], deleted["schedule"]), ("Dock", [second, third], 1, None))

    def test_reading_settings_writes_nothing(self):
        with connect() as db:
            before = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        for path in (f"/api/camera-groups/{self.group}/retention", f"/api/camera-groups/{self.group}/schedule", f"/api/camera-groups/{self.group}/members",
                     f"/api/cameras/{self.cameras[0]}/retention", f"/api/cameras/{self.cameras[0]}/schedule", "/api/auth/me"):
            self.assertEqual(self.admin.get(path).status_code, 200, path)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], before)


if __name__ == "__main__":
    import unittest

    unittest.main()
