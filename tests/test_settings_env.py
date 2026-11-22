"""A setting written in .env reaches the server, a mistyped number does not stop it, and container logs are bounded (#543, #544)."""
from __future__ import annotations

import importlib
import logging
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from app import env_numbers

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"NVR_[A-Z0-9_]+")
# Set by the NVR itself, by the image, or fixed by compose.yaml: the README lists them under "Not for you to set".
INTERNAL = {"NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_STORAGE_VOLUMES", "NVR_CREDENTIAL_KEY_PATH", "NVR_BACKUP_PATH", "NVR_LISTEN_PORT",
            "NVR_BUILD_COMMIT", "NVR_SUPERVISED", "NVR_STARTED_AT", "NVR_CONTROL_DIR"}
# Read by Compose to fill in compose.yaml, never by the NVR.
COMPOSE_ONLY = {"NVR_PORT"}
# Only the tests' own fixtures use it.
TEST_ONLY = {"NVR_BROWSER_FIXTURE_PASSWORD_PATH"}


def readme_sections() -> tuple[set[str], set[str]]:
    """The settings in the README's table, and those it lists as not for the administrator to set."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    table = {name for line in text.splitlines() if line.startswith("| `NVR_") for name in NAME.findall(line.split("|")[1])}
    internal = set(NAME.findall(next(line for line in text.splitlines() if line.startswith("**Not for you to set.**")))) - COMPOSE_ONLY  # (It says to change NVR_PORT instead)
    return table, internal


def compose_service(name: str = "compose.yaml", service: str = "nvr") -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))["services"][service]


def read_by_the_app() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        names.update(NAME.findall(path.read_text(encoding="utf-8")))
    return {name for name in names if not name.endswith("_")}  # (A prefix written in prose, such as NVR_OIDC_)


class SettingsReachTheServerTests(unittest.TestCase):
    def test_every_setting_in_the_readmes_table_is_passed_by_the_compose_file(self):
        table, _ = readme_sections()
        passed = set(compose_service()["environment"])
        self.assertEqual(sorted(table - passed - COMPOSE_ONLY - TEST_ONLY - {"NVR_OIDC_"}), [])

    def test_every_setting_the_app_reads_is_in_the_table_or_marked_internal(self):
        table, internal = readme_sections()
        self.assertEqual(internal, INTERNAL)
        self.assertEqual(sorted(read_by_the_app() - table - internal), [])
        self.assertEqual(sorted(internal & table), [])  # Never both

    def test_the_example_env_file_offers_only_settings_that_take_effect(self):
        passed = set(compose_service()["environment"])
        offered = set(NAME.findall((ROOT / ".env.example").read_text(encoding="utf-8")))
        self.assertEqual(sorted(offered - passed - COMPOSE_ONLY), [])
        # A setting fixed by the Compose file is not offered as though it could be changed there.
        fixed = {name for name, value in compose_service()["environment"].items() if value is not None and "${" not in str(value)}
        self.assertEqual(sorted(offered & fixed), [])

    def test_a_value_in_the_env_file_reaches_the_service_and_one_that_is_not_set_is_left_unset(self):
        if subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode != 0:
            self.skipTest("Docker Compose is not installed here; CI's build job runs it")
        env_file = ROOT / ".env.settings-test"
        env_file.write_text("NVR_UPDATE_CHECK=false\nNOT_FOR_THE_NVR=secret\n", encoding="utf-8")
        self.addCleanup(env_file.unlink, missing_ok=True)
        shown = subprocess.run(["docker", "compose", "--env-file", str(env_file), "config", "--format", "json"], cwd=ROOT, capture_output=True, text=True, check=True,
                               env={key: value for key, value in os.environ.items() if not key.startswith("NVR_")})
        import json

        environment = json.loads(shown.stdout)["services"]["nvr"]["environment"]
        self.assertEqual(environment["NVR_UPDATE_CHECK"], "false")
        self.assertIsNone(environment["NVR_HWACCEL"])  # Not set: not given to the container, so the default applies
        self.assertNotIn("NOT_FOR_THE_NVR", environment)  # Only what the file names goes in: a tunnel's token in .env is not handed to the NVR


class NumbersTests(unittest.TestCase):
    # Each numeric setting that used to be converted as its module loaded: the module, the name it lands in, the default, the least and most.
    SETTINGS = (("NVR_STOP_GRACE_SECONDS", "app.workers.supervisor", "STOP_GRACE_SECONDS", 25, 1, 600),
                ("NVR_TAMPER_SAMPLE_SECONDS", "app.tamper", "SAMPLE_SECONDS", 15, 1, 3600),
                ("NVR_SMART_INTERVAL_SECONDS", "app.smart", "REFRESH_SECONDS", 6 * 3600, 60, 7 * 86400),
                ("NVR_WORKER_START_SECONDS", "app.workers.liveness", "START_LIMIT_SECONDS", 300, 30, 3600))

    def loaded(self, variable: str, value: str, module: str, attribute: str) -> float:
        """What the setting comes to in a fresh interpreter: the module must load."""
        result = subprocess.run([sys.executable, "-c", f"import {module} as m; print(m.{attribute})"], cwd=ROOT, capture_output=True, text=True, check=False,
                                env={**os.environ, variable: value})
        self.assertEqual(result.returncode, 0, f"{variable}={value!r} stopped {module} loading: {result.stderr[-400:]}")
        return float(result.stdout.strip().splitlines()[-1])

    def test_abc_zero_and_minus_one_leave_every_module_loadable_with_a_sensible_value(self):
        for variable, module, attribute, default, least, most in self.SETTINGS:
            self.assertEqual(self.loaded(variable, "abc", module, attribute), default, variable)
            self.assertEqual(self.loaded(variable, "30s", module, attribute), default, variable)
            self.assertEqual(self.loaded(variable, "0", module, attribute), least, variable)
            self.assertEqual(self.loaded(variable, "-1", module, attribute), least, variable)
            self.assertEqual(self.loaded(variable, "1e12", module, attribute), most, variable)

    def test_the_settings_read_when_they_are_used_fall_back_too(self):
        from app import forecast

        for value, expected in (("abc", 1 << 30), ("0", 64 << 20), ("-1", 64 << 20), ("2147483648", 2147483648)):
            with patch.dict(os.environ, {"NVR_STORAGE_PRESSURE_FREE_BYTES": value}):
                self.assertEqual(forecast.reserve_bytes(), expected)
        with patch.dict(os.environ, {"NVR_ONVIF_TIMEOUT_SECONDS": "soon"}):
            import app.onvif_client as onvif_client

            self.assertEqual(importlib.reload(onvif_client).TIMEOUT.read, 8)
        importlib.reload(onvif_client)

    def test_a_rejected_value_is_said_once_by_name_with_the_value_used_and_never_printed_itself(self):
        env_numbers._said.clear()
        with patch.dict(os.environ, {"NVR_EXAMPLE_SECONDS": "hunter2-not-a-number"}), self.assertLogs("app.env_numbers", logging.WARNING) as logs:
            self.assertEqual(env_numbers.number("NVR_EXAMPLE_SECONDS", 15, 1, 60), 15)
            self.assertEqual(env_numbers.number("NVR_EXAMPLE_SECONDS", 15, 1, 60), 15)  # Read again: not said again
        self.assertEqual(len(logs.output), 1)
        self.assertIn("NVR_EXAMPLE_SECONDS", logs.output[0])
        self.assertIn("15", logs.output[0])
        self.assertNotIn("hunter2", logs.output[0])
        with patch.dict(os.environ, {"NVR_EXAMPLE_SECONDS": " 30 "}):
            self.assertEqual(env_numbers.number("NVR_EXAMPLE_SECONDS", 15, 1, 60), 30)  # A good value is simply used
        for unset in ("", "   "):
            with patch.dict(os.environ, {"NVR_EXAMPLE_SECONDS": unset}):
                self.assertEqual(env_numbers.number("NVR_EXAMPLE_SECONDS", 15, 1, 60), 15)  # Set but empty is unset: no line
        for odd in ("nan", "inf", "-inf"):
            env_numbers._said.clear()
            with patch.dict(os.environ, {"NVR_EXAMPLE_SECONDS": odd}), self.assertLogs("app.env_numbers", logging.WARNING):
                self.assertEqual(env_numbers.number("NVR_EXAMPLE_SECONDS", 15, 1, 60), 15)


class ContainerLogTests(unittest.TestCase):
    def test_every_service_in_every_compose_file_has_a_bounded_log(self):
        seen = []
        for file in sorted(ROOT.glob("compose*.yaml")):
            for name, service in yaml.safe_load(file.read_text(encoding="utf-8"))["services"].items():
                if "image" not in service:
                    continue  # An overlay's additions to the nvr service: the service itself, and its log, are compose.yaml's
                seen.append(f"{file.name}:{name}")
                logging_section = service.get("logging") or {}
                options = logging_section.get("options") or {}
                self.assertEqual(logging_section.get("driver"), "json-file", f"{file.name}:{name}")
                self.assertRegex(str(options.get("max-size")), r"^\d+m$", f"{file.name}:{name}")
                self.assertGreaterEqual(int(options.get("max-file", 0)), 2, f"{file.name}:{name}")
                megabytes = int(str(options["max-size"])[:-1]) * int(options["max-file"])
                self.assertLessEqual(megabytes, 200, f"{file.name}:{name}")
        self.assertEqual(seen, ["compose.cloudflared.yaml:cloudflared", "compose.tailscale.yaml:tailscale", "compose.wireguard.yaml:wireguard", "compose.yaml:nvr"])

    def test_the_access_log_holds_the_requests_that_failed_and_not_every_request(self):
        import logging.config

        from app.workers import web

        config = web.log_config()
        self.assertEqual(config["handlers"]["access"]["filters"], ["failed_requests_only"])
        logging.config.dictConfig(config)
        self.addCleanup(logging.config.dictConfig, {"version": 1, "disable_existing_loggers": False})
        keep = web.FailedRequestsOnly()

        def line(status: int) -> logging.LogRecord:
            return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ("192.0.2.10:5000", "GET", "/api/cameras", "1.1", status), None)

        self.assertEqual([status for status in (200, 204, 304, 399, 400, 401, 404, 500, 503) if keep.filter(line(status))], [400, 401, 404, 500, 503])
        odd = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "something else", None, None)
        self.assertTrue(keep.filter(odd))  # Not a request line: never swallowed


class SecretsInTheRepositoryFolderTests(unittest.TestCase):
    """#550: the guides put the site's WireGuard configuration, with its private key, inside the repository's folder. It can never be added by accident."""

    def test_the_wireguard_folder_is_ignored_by_git_and_left_out_of_the_image(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("deploy/wireguard/", ignored)
        self.assertIn(".env", ignored)  # Where the Tailscale key and the Cloudflare token go
        self.assertIn("deploy/wireguard", (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
        self.assertIn(".env", (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
        if (ROOT / ".git").exists():
            checked = subprocess.run(["git", "check-ignore", "deploy/wireguard/wg_confs/wg0.conf"], cwd=ROOT, capture_output=True, check=False)
            self.assertEqual(checked.returncode, 0)
        # The overlay mounts that folder and nothing else of the kind from the repository; Tailscale's state is a named volume.
        wireguard = yaml.safe_load((ROOT / "compose.wireguard.yaml").read_text(encoding="utf-8"))["services"]["wireguard"]["volumes"]
        self.assertIn("./deploy/wireguard:/config", wireguard)
        tailscale = yaml.safe_load((ROOT / "compose.tailscale.yaml").read_text(encoding="utf-8"))["services"]["tailscale"]["volumes"]
        self.assertIn("tailscale-state:/var/lib/tailscale", tailscale)
