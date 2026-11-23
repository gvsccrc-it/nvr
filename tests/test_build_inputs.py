"""The build's inputs cannot move under it, and CI checks every merge (#549).

A download address that names "latest" or a branch can be replaced upstream while its pinned hash stays the same, and then the image fails
to build for every pull request and for main at once. The camera simulator CI runs was pinned by tag alone. Runs on main shared one
concurrency group, and GitHub keeps one waiting run per group, so a third merge inside one run's time cancelled the second."""
from __future__ import annotations

import fnmatch
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
MOVING = re.compile(r"/(latest|main|master|develop|HEAD)(/|$)")


def workflow() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


class DownloadTests(unittest.TestCase):
    def test_no_download_address_names_latest_or_a_branch(self):
        addresses = re.findall(r"https://\S+", DOCKERFILE)
        self.assertGreaterEqual(len(addresses), 9)
        for address in addresses:
            with self.subTest(address=address):
                self.assertIsNone(MOVING.search(address), f"{address} can change while its hash stays pinned")

    def test_the_model_registries_use_the_same_fixed_addresses(self):
        from app import face_models, plate_models, pose_models

        for registry in (face_models, pose_models, plate_models):
            for value in vars(registry).values():
                url = getattr(value, "url", None)
                if isinstance(url, str):
                    with self.subTest(url=url):
                        self.assertIsNone(MOVING.search(url))
                        self.assertIn(url, DOCKERFILE)


class CameraSimulatorTests(unittest.TestCase):
    def test_the_simulator_image_is_pinned_by_digest_and_dependabot_watches_it(self):
        compose = yaml.safe_load((ROOT / ".github" / "camera-simulator" / "compose.yaml").read_text(encoding="utf-8"))
        self.assertRegex(compose["services"]["mediamtx"]["image"], r"^bluenviron/mediamtx:[\d.]+@sha256:[0-9a-f]{64}$")
        dependabot = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
        self.assertIn(("docker-compose", "/.github/camera-simulator"), {(item["package-ecosystem"], item["directory"]) for item in dependabot["updates"]})

    def test_every_job_that_needs_it_starts_it_from_that_file_and_none_names_an_image_of_its_own(self):
        jobs = workflow()["jobs"]
        for name, job in jobs.items():
            with self.subTest(job=name):
                self.assertNotIn("services", job)
                steps = " ".join(step.get("run", "") for step in job.get("steps", []))
                if "rtsp://127.0.0.1:8554" in steps:
                    self.assertIn("docker compose -f .github/camera-simulator/compose.yaml up -d --wait", steps)


class ConcurrencyTests(unittest.TestCase):
    def test_only_pull_requests_share_a_group_and_only_they_are_cancelled(self):
        rule = workflow()["concurrency"]
        self.assertEqual(rule["cancel-in-progress"], "${{ github.event_name == 'pull_request' }}")
        # A pull request's runs share its ref; every other run (a merge to main, a run by hand) has its own group, so none can be
        # cancelled while it waits
        self.assertEqual(rule["group"], "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}")

    def test_edge_moves_only_for_mains_newest_commit_and_every_commit_gets_its_own_tag(self):
        publish = workflow()["jobs"]["publish"]
        script = next(step["run"] for step in publish["steps"] if step.get("name") == "Publish the tested image")
        self.assertLess(script.index('docker push "ghcr.io/gvsccrc-it/nvr:sha-$short"'), script.index("commits/main"))
        self.assertRegex(script, r'if \[ "\$newest" = "\$GITHUB_SHA" \]; then\n\s+docker push ghcr.io/gvsccrc-it/nvr:edge')


class DockerignoreTests(unittest.TestCase):
    def test_compiled_python_is_left_out_at_every_depth(self):
        patterns = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
        for path in ("app/__pycache__", "app/api/__pycache__", "app/api/x.pyc", "tests/fixtures/y.pyo", "__pycache__"):
            with self.subTest(path=path):
                # Docker's matcher treats ** as any number of folders, including none
                self.assertTrue(any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.replace("**/", "")) for pattern in patterns), path)


if __name__ == "__main__":
    unittest.main()
