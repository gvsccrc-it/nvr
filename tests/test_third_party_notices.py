"""The repository and its image say on what terms they come, what they carry from others, and how to report a vulnerability (#552).

Every file the Dockerfile downloads and every package it installs with apt must be listed in THIRD_PARTY_NOTICES.md, and the notices and the
licence statement must be copied into the image, so a model or a program added to the build cannot be shipped without a notice."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
NOTICES = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")


class NoticeTests(unittest.TestCase):
    def test_every_file_the_dockerfile_downloads_is_listed_with_a_licence_and_a_source(self):
        downloads = re.findall(r"curl [^\n]*? (https://\S+) -o (\S+)", DOCKERFILE)
        self.assertGreaterEqual(len(downloads), 9)
        for url, target in downloads:
            with self.subTest(file=Path(target).name):
                name = Path(target).name
                row = next((line for line in NOTICES.splitlines() if f"`{name}`" in line and line.startswith("|")), None)
                self.assertIsNotNone(row, f"{name} is downloaded by the Dockerfile and not listed in THIRD_PARTY_NOTICES.md")
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                self.assertEqual(len(cells), 4, row)
                self.assertTrue(cells[2] and cells[3], f"{name} needs a licence and a source")

    def test_every_package_the_dockerfile_installs_from_debian_is_listed(self):
        installed = re.search(r"apt-get install -y --no-install-recommends ([a-z0-9 .+-]+?) *\\", DOCKERFILE)
        self.assertIsNotNone(installed)
        packages = installed.group(1).split()
        self.assertIn("ffmpeg", packages)
        for package in packages:
            with self.subTest(package=package):
                self.assertRegex(NOTICES, rf"(?m)^\| `{re.escape(package)}` \|", f"{package} is installed by the Dockerfile and not listed")

    def test_ffmpeg_is_listed_as_gpl_with_a_pointer_to_its_source(self):
        row = next(line for line in NOTICES.splitlines() if line.startswith("| `ffmpeg` |"))
        self.assertIn("GPL", row)
        self.assertIn("sources.debian.org/src/ffmpeg", row)

    def test_the_image_carries_the_notices_and_the_licence(self):
        self.assertRegex(DOCKERFILE, r"(?m)^COPY LICENSE THIRD_PARTY_NOTICES\.md \./$")
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
        self.assertFalse({"LICENSE", "THIRD_PARTY_NOTICES.md", "*.md"} & set(ignored))

    def test_the_licence_statement_security_policy_and_code_owners_exist(self):
        self.assertIn("All rights reserved", (ROOT / "LICENSE").read_text(encoding="utf-8"))
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("Report a vulnerability", security)
        self.assertIn("privately", security)
        owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        self.assertRegex(owners, r"(?m)^\* @\S+$")

    def test_the_plate_detectors_licence_position_is_recorded(self):
        plates = (ROOT / "docs" / "licence-plates.md").read_text(encoding="utf-8")
        section = plates[plates.index("### The plate detector's licence"):]
        for fact in ("yolo-v9-t-640-license-plates-end2end.onnx", "open-image-models", "MIT", "GPL-3.0", "No legal advice has been taken"):
            self.assertIn(fact, section)


if __name__ == "__main__":
    unittest.main()
