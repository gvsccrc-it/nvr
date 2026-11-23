"""The backup passphrase and the sign-in secrets are not handed to every worker and every FFmpeg process (#547).

The supervisor copied its whole environment to all six workers, and every FFmpeg, ffprobe and smartctl process inherited it. FFmpeg parses
what cameras send, so a flaw in it was a flaw in a process holding the backup passphrase."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import child_env

ROOT = Path(__file__).resolve().parent.parent
SECRETS = {"NVR_BACKUP_PASSPHRASE": "a backup passphrase for the test", "NVR_OIDC_CLIENT_SECRET": "client-secret-for-the-test",
           "NVR_OIDC_SESSION_SECRET": "s" * 40, "NVR_LOG_FORWARD_HEADERS": "Authorization=Bearer test-token",
           "NVR_BACKUP_PASSPHRASE_FILE": "/run/secrets/passphrase", "SOME_API_TOKEN": "x", "AWS_SECRET_ACCESS_KEY": "y"}
FORBIDDEN = ("SECRET", "PASSPHRASE", "HEADERS")


class RecordingChildTests(unittest.TestCase):
    """A real child: the continuous recorder starts a stand-in FFmpeg that writes down the environment it was given."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        settings = {"NVR_DB_PATH": str(self.root / "state.sqlite3"), "NVR_RECORDINGS_PATH": str(self.root / "recordings"),
                    "NVR_CREDENTIAL_KEY_PATH": str(self.root / "credentials.key"), "LIBVA_DRIVER_NAME": "iHD", "LC_ALL": "C.UTF-8", **SECRETS}
        patcher = patch.dict(os.environ, settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        from app.store import initialize

        initialize()

    def test_a_recording_child_is_given_no_secret_and_keeps_what_acceleration_needs(self):
        from app.credentials import encrypt
        from app.recording import RecordingManager
        from app.store import connect

        camera_id = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_enabled,recording_mode) VALUES(?,?,?,1,'continuous')",
                       (camera_id, f"Environment {camera_id}", encrypt("rtsp://127.0.0.1:9/stream")))
        written = self.root / "environment.json"
        fake = self.root / "ffmpeg"
        fake.write_text(f"#!/usr/bin/env python3\nimport json, os, sys\nopen({str(written)!r}, 'w').write(json.dumps(dict(os.environ)))\nsys.stdin.read()\n",
                        encoding="utf-8")
        fake.chmod(0o755)
        manager = RecordingManager()

        async def stream_fault(source):
            return "camera_offline"

        async def body():
            task = asyncio.create_task(manager._run_continuous(camera_id))
            for _ in range(200):
                if written.exists():
                    break
                await asyncio.sleep(0.05)
            manager.stopping = True
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)

        with patch("app.recording.shutil.which", side_effect=lambda name: str(fake)), patch("app.recording._stream_fault", stream_fault):
            asyncio.run(body())
        environment = json.loads(written.read_text(encoding="utf-8"))
        leaked = [name for name in environment if any(word in name.upper() for word in FORBIDDEN)]
        self.assertEqual(leaked, [])
        self.assertNotIn("SOME_API_TOKEN", environment)
        self.assertNotIn("NVR_DB_PATH", environment)  # Only the listed variables, not the NVR's settings
        self.assertEqual((environment["TZ"], environment["LIBVA_DRIVER_NAME"], environment["LC_ALL"]), ("UTC", "iHD", "C.UTF-8"))
        self.assertEqual(environment["PATH"], os.environ["PATH"])


class HelperTests(unittest.TestCase):
    def test_the_child_environment_is_the_listed_variables_and_never_a_secret_looking_one(self):
        with patch.dict(os.environ, {**SECRETS, "CUDA_VISIBLE_DEVICES": "0", "NVIDIA_DRIVER_CAPABILITIES": "video", "MFX_IMPL": "hw",
                                     "LIBVA_DRIVERS_PATH": "/usr/lib/dri", "CUDA_AUTH_TOKEN": "z", "NVR_RECORDINGS_PATH": "/recordings"}):
            environment = child_env.child_environment()
        self.assertTrue({"CUDA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES", "MFX_IMPL", "LIBVA_DRIVERS_PATH", "PATH"} <= set(environment))
        self.assertNotIn("CUDA_AUTH_TOKEN", environment)  # A listed prefix, a secret-looking name: left out
        self.assertNotIn("NVR_RECORDINGS_PATH", environment)
        self.assertFalse([name for name in environment if any(word in name.upper() for word in FORBIDDEN)])

    def test_each_worker_gets_only_the_secrets_it_uses(self):
        with patch.dict(os.environ, SECRETS):
            given = {worker: child_env.worker_environment(worker) for worker in ("recorder", "exporter", "ai", "rules", "background", "web")}
        for worker in ("recorder", "exporter", "ai", "rules"):
            self.assertFalse({"NVR_BACKUP_PASSPHRASE", "NVR_BACKUP_PASSPHRASE_FILE", "NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET"} & set(given[worker]), worker)
        self.assertEqual(given["background"]["NVR_BACKUP_PASSPHRASE"], SECRETS["NVR_BACKUP_PASSPHRASE"])
        self.assertNotIn("NVR_OIDC_CLIENT_SECRET", given["background"])
        self.assertEqual((given["web"]["NVR_OIDC_CLIENT_SECRET"], given["web"]["NVR_BACKUP_PASSPHRASE"]), (SECRETS["NVR_OIDC_CLIENT_SECRET"], SECRETS["NVR_BACKUP_PASSPHRASE"]))
        self.assertEqual(given["recorder"]["NVR_LOG_FORWARD_HEADERS"], SECRETS["NVR_LOG_FORWARD_HEADERS"])  # Every worker forwards its own log

    def test_the_supervisor_starts_workers_with_their_own_environment(self):
        from app.workers import supervisor

        started = []
        def popen(args, **kwargs):
            started.append(kwargs["env"])
            return SimpleNamespace(pid=12345)

        with patch.dict(os.environ, SECRETS), patch.object(supervisor.subprocess, "Popen", side_effect=popen), patch.object(supervisor, "_record", lambda *a, **k: None):
            supervisor.Worker("recorder", "app.workers.recorder").start()
        self.assertEqual(started[0]["NVR_SUPERVISED"], "1")
        self.assertNotIn("NVR_BACKUP_PASSPHRASE", started[0])
        self.assertNotIn("NVR_OIDC_CLIENT_SECRET", started[0])

    def test_a_secret_comes_from_its_variable_as_set_or_else_from_its_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            held = Path(temporary) / "secret"
            held.write_text("from-the-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"NVR_OIDC_CLIENT_SECRET_FILE": str(held)}, clear=False):
                os.environ.pop("NVR_OIDC_CLIENT_SECRET", None)
                self.assertEqual(child_env.secret("NVR_OIDC_CLIENT_SECRET"), "from-the-file")
                os.environ["NVR_OIDC_CLIENT_SECRET"] = " exactly as set "
                self.assertEqual(child_env.secret("NVR_OIDC_CLIENT_SECRET"), " exactly as set ")
            with patch.dict(os.environ, {"NVR_OIDC_SESSION_SECRET_FILE": str(Path(temporary) / "missing")}):
                os.environ.pop("NVR_OIDC_SESSION_SECRET", None)
                self.assertEqual(child_env.secret("NVR_OIDC_SESSION_SECRET"), "")

    def test_the_sign_in_secrets_and_the_passphrase_can_come_from_files(self):
        from app import backup
        from app.api import identity

        with tempfile.TemporaryDirectory() as temporary:
            files = {}
            for name, value in (("client", "client-secret-from-a-file"), ("session", "s" * 48), ("passphrase", "a passphrase from a file, long enough")):
                files[name] = Path(temporary) / name
                files[name].write_text(value + "\n", encoding="utf-8")
            settings = {"NVR_PUBLIC_URL": "https://nvr.example.test", "NVR_OIDC_TENANT_ID": str(uuid.uuid4()), "NVR_OIDC_CLIENT_ID": "client-id",
                        "NVR_OIDC_ADMIN_ROLES": "NVR.Admin", "NVR_OIDC_CLIENT_SECRET_FILE": str(files["client"]),
                        "NVR_OIDC_SESSION_SECRET_FILE": str(files["session"]), "NVR_BACKUP_PASSPHRASE_FILE": str(files["passphrase"])}
            with patch.dict(os.environ, settings):
                for name in ("NVR_OIDC_CLIENT_SECRET", "NVR_OIDC_SESSION_SECRET", "NVR_BACKUP_PASSPHRASE"):
                    os.environ.pop(name, None)
                found = identity._oidc_settings()
                self.assertIsNotNone(found)
                self.assertEqual(found["client_secret"], "client-secret-from-a-file")
                self.assertEqual(backup.passphrase(), "a passphrase from a file, long enough")


class EveryChildTests(unittest.TestCase):
    def test_every_process_the_nvr_starts_is_given_an_environment(self):
        """Through the one helper (or, for a worker, the supervisor's per-worker one): a process started without ``env=`` inherits everything."""
        found = []
        for path in sorted((ROOT / "app").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                if name.endswith(("create_subprocess_exec", "create_subprocess_shell")) or name in ("subprocess.run", "subprocess.Popen", "subprocess.check_output", "subprocess.check_call"):
                    env = next((keyword.value for keyword in node.keywords if keyword.arg == "env"), None)
                    found.append((f"{path.relative_to(ROOT)}:{node.lineno}", ast.unparse(env) if env is not None else None))
        self.assertGreater(len(found), 40)
        for where, env in found:
            with self.subTest(where=where):
                self.assertIsNotNone(env, f"{where} starts a process with the whole environment")
                self.assertTrue(env.startswith(("child_environment(", "env", "environment")), f"{where}: env={env}")


if __name__ == "__main__":
    unittest.main()
