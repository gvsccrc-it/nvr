"""The first visitor cannot claim the administrator account without the one-time setup code (#542)."""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from unittest.mock import patch

from app import setup_code
from app.store import connect

import unittest

from tests import test_bootstrap
from tests.test_support import TEST_PASSWORD


class SetupCodeTests(unittest.TestCase):
    setUp = test_bootstrap.BootstrapTests.setUp  # The same isolated installation the bootstrap tests use, without running their tests again
    tearDown = test_bootstrap.BootstrapTests.tearDown

    def create(self, code: str | None, username: str = "local-admin", **headers):
        body = {"username": username, "password": TEST_PASSWORD}
        if code is not None:
            body["setup_code"] = code
        return self.client.post("/api/setup/admin", json=body, headers=headers)

    def audit(self, action: str) -> list[dict]:
        with connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT details_json FROM audit_log WHERE action=? ORDER BY id", (action,))]

    def users(self) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def test_without_the_code_or_with_a_wrong_one_setup_is_refused_and_with_it_it_works_once(self):
        code = setup_code.expected()
        self.assertRegex(code, r"^[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}$")
        for wrong in (None, "", "AAAA-BBBB-CCCC", code[:-1], code + "X"):
            refused = self.create(wrong)
            self.assertEqual(refused.status_code, 403, wrong)
            self.assertIn("docker compose logs nvr", refused.json()["detail"])  # Where to find it
            self.assertNotIn(code, refused.text)  # Never the code itself
        self.assertEqual(self.users(), 0)
        with patch.object(setup_code, "WRONG_A_MINUTE", 99):  # (The limit has its own test below)
            created = self.create(code.lower().replace("-", " "))  # As people type it: either case, spaces or dashes
        self.assertEqual(created.status_code, 201, created.text)
        self.assertNotIn(code, created.text)
        self.assertEqual(self.users(), 1)
        # It worked once: the file is gone, there is no code any more, and setup is closed.
        self.assertFalse(setup_code.path().exists())
        self.assertIsNone(setup_code.expected())
        self.assertEqual(self.create(code, "second-admin").status_code, 409)
        self.assertEqual(self.users(), 1)

    def test_the_code_is_never_in_an_answer_or_a_security_log_row_and_each_wrong_code_is_logged(self):
        code = setup_code.expected()
        status = self.client.get("/api/setup/status")
        self.assertEqual(status.json(), {"setup_required": True})
        self.assertNotIn(code, status.text)
        self.create("WRNG-WRNG-WRNG")
        self.create("ALSO-WRNG-CODE")
        self.assertEqual(self.create(code).status_code, 201)
        refused = self.audit("setup_code_refused")
        self.assertEqual(len(refused), 2)
        self.assertEqual(set(refused[0]), {"address"})  # Who tried, never what they tried
        self.assertEqual([entry["username"] for entry in self.audit("setup_completed")], ["local-admin"])
        with connect() as db:
            everything = " ".join(str(value) for row in db.execute("SELECT * FROM audit_log") for value in tuple(row))
        for secret in (code, "WRNG-WRNG-WRNG", "ALSO-WRNG-CODE", setup_code.normalise(code)):
            self.assertNotIn(secret, everything)
        log = self.client.get("/api/admin/security-log", params={"start": "2020-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z"})
        if log.status_code == 200:
            self.assertNotIn(code, log.text)
            self.assertIn("setup_code_refused", log.text)

    def test_five_wrong_codes_a_minute_and_then_even_the_right_one_waits(self):
        code = setup_code.expected()
        for attempt in range(setup_code.WRONG_A_MINUTE):
            self.assertEqual(self.create(f"NOPE-NOPE-{attempt:04d}").status_code, 403)
        limited = self.create(code)
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Wait a minute", limited.json()["detail"])
        self.assertEqual(self.users(), 0)
        self.assertEqual(len(self.audit("setup_code_refused")), setup_code.WRONG_A_MINUTE)  # A refusal for the limit is not another wrong code
        # The limit is for everyone together as well as each address: a guesser spread over many addresses gets the same five.
        counted = setup_code._wrong[str(setup_code.path())]
        self.assertEqual(len(counted[""]), setup_code.WRONG_A_MINUTE)
        real = setup_code.time.monotonic
        with patch.object(setup_code.time, "monotonic", lambda: real() + setup_code.WINDOW_SECONDS + 1):  # A minute on
            self.assertEqual(self.create(code).status_code, 201)

    def test_the_code_is_new_at_each_start_printed_once_and_kept_where_only_the_service_user_can_read_it(self):
        first = setup_code.expected()
        with self.assertLogs("app.setup_code", logging.WARNING) as logs:
            second = setup_code.announce()  # The NVR starts again, still with no administrator
        self.assertNotEqual(first, second)
        printed = [line for line in logs.output if second in line]
        self.assertEqual(len(printed), 1)
        self.assertIn("setup code", printed[0])
        self.assertEqual(setup_code.path().read_text(encoding="utf-8").strip(), second)
        self.assertEqual(stat.S_IMODE(setup_code.path().stat().st_mode), 0o600)
        self.assertEqual(setup_code.path().parent, Path(os.environ["NVR_CREDENTIAL_KEY_PATH"]).parent)  # The secrets folder
        self.assertEqual(self.create(first).status_code, 403)  # The earlier start's code is dead
        self.assertEqual(self.create(second).status_code, 201)
        # Once an administrator exists a start makes no code and leaves none behind.
        setup_code.path().write_text("LEFT-OVER-CODE\n", encoding="utf-8")
        with self.assertNoLogs("app.setup_code", logging.WARNING):
            self.assertIsNone(setup_code.announce())
        self.assertFalse(setup_code.path().exists())

    def test_an_empty_database_beside_existing_footage_is_called_out_as_probably_lost(self):
        with self.assertNoLogs("app.setup_code", logging.ERROR):
            setup_code.announce()  # A new installation: nothing recorded yet
        footage = Path(os.environ["NVR_RECORDINGS_PATH"]) / "a-camera" / "segment.mp4"
        footage.parent.mkdir(parents=True)
        footage.write_bytes(b"footage from before")
        with self.assertLogs("app.setup_code", logging.ERROR) as logs:
            code = setup_code.announce()
        self.assertIn("looks lost or misplaced", logs.output[0])
        self.assertIn("docs/database-recovery.md", logs.output[0])
        self.assertEqual(self.create(code).status_code, 201)  # The code still applies: the installer decides

    def test_the_code_comes_before_anything_else_is_looked_at(self):
        # A caller without the code learns nothing about what a username or password must be like here.
        refused = self.client.post("/api/setup/admin", json={"username": "local-admin", "password": "longpasswordonly", "setup_code": "AAAA-BBBB-CCCC"})
        self.assertEqual(refused.status_code, 403)
        self.assertNotIn("letter", refused.text)
        accepted_code = self.client.post("/api/setup/admin", json={"username": "local-admin", "password": "longpasswordonly", "setup_code": setup_code.expected()})
        self.assertEqual(accepted_code.status_code, 422)  # With it, the password's own problem is explained
        self.assertEqual(self.users(), 0)
        self.assertTrue(setup_code.path().exists())  # And the code is still good: nothing was created
