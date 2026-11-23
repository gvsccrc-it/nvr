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


class SignedImageTests(unittest.TestCase):
    """#548: what is published is signed, recorded and scanned; releases are tagged by digest; a weekly rebuild picks up system fixes."""

    def test_the_build_scans_the_images_system_packages_with_a_checked_scanner(self):
        steps = {step.get("name"): step.get("run", "") for step in workflow()["jobs"]["build"]["steps"]}
        install = steps["Install Trivy"]
        self.assertRegex(install, r"releases/download/v[\d.]+/trivy_[\d.]+_Linux-64bit\.tar\.gz")
        self.assertRegex(install, r"echo '[0-9a-f]{64}  /tmp/trivy\.tgz' \| sha256sum -c -")
        scan = steps["Scan the image's system packages"]
        self.assertIn("--pkg-types os --severity HIGH,CRITICAL --format json", scan)
        self.assertIn("check-vulnerabilities.py --image /tmp/nvr-image-scan.json", scan)
        names = list(steps)
        self.assertLess(names.index("Build container image"), names.index("Scan the image's system packages"))

    def test_main_is_rebuilt_weekly_and_published_like_a_merge(self):
        flow = workflow()
        triggers = flow.get("on") or flow.get(True)  # YAML 1.1 reads a bare "on" as true
        self.assertEqual(len(triggers["schedule"]), 1)
        self.assertIn("apt-get upgrade -y", DOCKERFILE)  # Otherwise a rebuild would not carry the base image's own fixes
        publish = flow["jobs"]["publish"]
        self.assertIn("github.event_name == 'schedule'", publish["if"])
        script = next(step["run"] for step in publish["steps"] if step.get("name") == "Publish the tested image")
        self.assertIn('if [ "$EVENT" != schedule ]; then', script)  # sha-<commit> stays the image tested at merge

    def test_what_is_published_is_signed_and_attested_by_its_digest(self):
        publish = workflow()["jobs"]["publish"]
        self.assertEqual((publish["permissions"]["id-token"], publish["permissions"]["attestations"]), ("write", "write"))
        steps = {step.get("name"): step for step in publish["steps"]}
        self.assertIn('cosign sign --yes "ghcr.io/gvsccrc-it/nvr@$DIGEST"', steps["Sign the image"]["run"])
        for name in ("Record how the image was built", "Record what is in the image"):
            self.assertEqual(steps[name]["with"]["subject-digest"], "${{ steps.publish.outputs.digest }}")
            self.assertTrue(steps[name]["with"]["push-to-registry"])
        for step in publish["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}$", step["uses"])

    def test_a_release_verifies_the_signature_and_tags_the_digest_it_names(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn('cosign verify "ghcr.io/gvsccrc-it/nvr@$digest"', release)
        self.assertIn('imagetools create --tag "ghcr.io/gvsccrc-it/nvr:$version" "ghcr.io/gvsccrc-it/nvr@$digest"', release)
        self.assertIn('"digest": "%s"', release)
        self.assertLess(release.index("cosign verify"), release.index("imagetools create"))


class DockerignoreTests(unittest.TestCase):
    def test_compiled_python_is_left_out_at_every_depth(self):
        patterns = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
        for path in ("app/__pycache__", "app/api/__pycache__", "app/api/x.pyc", "tests/fixtures/y.pyo", "__pycache__"):
            with self.subTest(path=path):
                # Docker's matcher treats ** as any number of folders, including none
                self.assertTrue(any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.replace("**/", "")) for pattern in patterns), path)


if __name__ == "__main__":
    unittest.main()
