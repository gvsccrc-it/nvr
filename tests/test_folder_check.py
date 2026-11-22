"""A folder the NVR was given but cannot write to is said at start-up, not at the first backup (#551)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app import folder_check
from app.store import connect, initialize

ROOT = Path(__file__).resolve().parents[1]


class FolderCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.backups = base / "offsite-backups"
        self.backups.mkdir()
        (base / "recordings" / "archive").mkdir(parents=True)
        environment = {"NVR_DB_PATH": str(base / "data" / "nvr.sqlite3"), "NVR_RECORDINGS_PATH": str(base / "recordings"), "NVR_BACKUP_PATH": str(self.backups),
                       "NVR_CREDENTIAL_KEY_PATH": str(base / "secrets" / "credentials.key"), "NVR_STORAGE_VOLUMES": json.dumps({"default": "", "archive": "archive"})}
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        (base / "secrets").mkdir()
        initialize()

    def incidents(self) -> dict[str, int]:
        with connect() as db:
            return {row["incident_key"]: row["fault_active"] for row in db.execute("SELECT incident_key,fault_active FROM incidents WHERE fault_type=?", (folder_check.FAULT_TYPE,))}

    def test_every_folder_the_nvr_was_given_is_tried_and_a_good_start_writes_nothing(self):
        self.assertEqual(set(folder_check.folders()), {"the database", "secrets", "backups", "recordings (default)", "recordings (archive)"})
        with self.assertNoLogs("app.folder_check", logging.ERROR):
            self.assertEqual(folder_check.check(), {})
        self.assertEqual(self.incidents(), {})
        self.assertEqual(list(self.backups.iterdir()), [])  # The probe file is not left behind

    def test_a_folder_that_cannot_be_written_gets_one_plain_line_and_an_incident_that_recovers(self):
        # A read-only folder, as a host folder owned by someone else is to the NVR's user. (Refused by the folder itself where the test
        # runs as an ordinary user; where it runs as root, which ignores permissions, the write is made to fail the same way.)
        self.backups.chmod(0o500)
        self.addCleanup(self.backups.chmod, 0o700)
        real = Path.write_bytes

        def refused(path: Path, data: bytes):
            if path.parent == self.backups:
                raise PermissionError(13, "Permission denied")
            return real(path, data)

        with patch.object(Path, "write_bytes", refused if os.geteuid() == 0 else real):
            with self.assertLogs("app.folder_check", logging.ERROR) as logs:
                failed = folder_check.check()
        self.assertEqual(failed, {"backups": "Permission denied"})
        (line,) = logs.output
        self.assertIn(str(self.backups), line)
        self.assertIn(f"sudo chown -R {os.getuid()}:{os.getgid()}", line)  # The user it runs as, and the command that fixes it
        self.assertEqual(self.incidents(), {"folder:backups": 1})
        with connect() as db:
            summary = db.execute("SELECT summary FROM incidents WHERE incident_key='folder:backups'").fetchone()[0]
        self.assertIn("cannot write to its backups folder", summary)
        # The folder is given to the NVR's user and it starts again: the incident recovers, and nothing else is raised.
        self.backups.chmod(0o700)
        self.assertEqual(folder_check.check(), {})
        self.assertEqual(self.incidents(), {"folder:backups": 0})

    def test_a_folder_that_is_not_there_is_left_to_the_check_that_owns_it(self):
        # A recordings volume that is not mounted has its own state and incident; a backups folder that does not exist is made at the first backup.
        self.backups.rmdir()
        self.assertEqual(folder_check.check(), {})
        self.assertEqual(self.incidents(), {})

    def test_the_image_makes_every_mounted_folder_its_users_and_the_docs_give_the_command(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("mkdir -p /data /secrets /recordings /recordings/archive /backups", dockerfile)  # The archive volume is mounted inside the recordings volume
        self.assertIn("chown -R nvr:nvr /app /data /secrets /recordings /backups", dockerfile)
        self.assertIn("--uid 10001", dockerfile)
        mounted = {volume.split(":")[1] for volume in yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]["nvr"]["volumes"]}
        self.assertEqual(mounted, {"/data", "/secrets", "/recordings", "/recordings/archive", "/backups"})
        for name in ("README.md", "compose.yaml"):
            self.assertIn("chown -R 10001:10001", (ROOT / name).read_text(encoding="utf-8"), name)
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("for folder in /data /secrets /recordings /recordings/archive /backups", workflow)  # The smoke test writes to each
