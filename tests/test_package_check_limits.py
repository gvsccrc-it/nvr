"""A small uploaded "package" cannot exhaust memory or disk while it is checked (#503).

Each test uploads or checks a small crafted package. A zip can declare far more than it holds (repetitive JSON compresses about 1,000 to 1),
so the server used to inflate a 2 GiB ``integrity.json`` from a 2 MB upload; nesting too deep raised an uncaught RecursionError; any number
of uploads could fill the recordings volume past its headroom; and one left by a killed web worker counted against the exports' budget."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import tracemalloc
import unittest
import zipfile
from collections import namedtuple
from pathlib import Path
from unittest import mock

from app import cases, copies_budget, exports, package_checks
from app.recording import recordings_path
from tests.test_access_grants import GrantCase
from tests.test_evidence_memory import evidence_package

MIB = 1024 * 1024


def deflated(members: dict[str, bytes], path: Path | None = None) -> bytes | None:
    """A zip of these members, compressed: a member of repeated bytes declares far more than it takes."""
    target = path or io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return None if path else target.getvalue()


def bomb(megabytes: int) -> bytes:
    """Valid JSON of that size that compresses to almost nothing."""
    return b'{"files": [' + b'"x",' * (megabytes * MIB // 4) + b'"x"]}'


def case_package(clips: list[bytes]) -> bytes:
    """A case package holding these clip packages, its manifest and identity as the NVR writes them."""
    metadata = json.dumps({"case_id": "case-1", "package_id": "package-1", "audit_reference": {"resource_type": "case", "resource_id": "case-1"}, "omitted_items": 0}).encode()
    document = json.dumps({"items": [{"kind": "clip", "id": f"item-{number}", "export_id": f"clip-{number}", "package_file": f"clips/{number}.zip"}
                                     for number in range(len(clips))]}).encode()
    members = {"metadata.json": metadata, "case.json": document, "notes.json": b"[]", "notes.md": b"", "audit.json": b"[]",
               **{f"clips/{number}.zip": body for number, body in enumerate(clips)}}
    integrity = {"algorithm": "sha256", "files": [{"path": name, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()} for name, body in members.items()]}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
        archive.writestr("integrity.json", json.dumps(integrity))
    return output.getvalue()


class CraftedPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        for key, value in (("NVR_RECORDINGS_PATH", Path(self.temporary.name) / "recordings"), ("NVR_DB_PATH", Path(self.temporary.name) / "state.sqlite3")):
            self.addCleanup(lambda key=key, was=os.environ.get(key): os.environ.pop(key, None) if was is None else os.environ.__setitem__(key, was))
            os.environ[key] = str(value)
        from app.store import initialize
        initialize()

    def bounded(self, work) -> dict:
        """Run a check, and fail if it took more than a few MiB or more than a few seconds."""
        tracemalloc.start()
        started = time.monotonic()
        try:
            result = work()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * MIB, f"{peak // MIB} MiB held")
        self.assertLess(time.monotonic() - started, 5)
        return result

    def test_an_integrity_manifest_declaring_far_more_than_a_genuine_one_is_refused_without_being_inflated(self):
        upload = deflated({"integrity.json": bomb(200), "metadata.json": b"{}"})
        self.assertLess(len(upload), 2 * MIB)  # The ticket's case: 200 MB of JSON in a small upload
        result = self.bounded(lambda: exports.verify_evidence_bytes(upload, "job-1"))
        self.assertFalse(result["checksums_valid"])
        self.assertIn("integrity.json is larger than a genuine package's", result["error"])

    def test_metadata_and_chain_are_bounded_as_well(self):
        manifest = lambda *names: json.dumps({"algorithm": "sha256", "files": [{"path": name, "size_bytes": 0, "sha256": ""} for name in names]}).encode()  # noqa: E731
        metadata_bomb = deflated({"integrity.json": manifest("metadata.json"), "metadata.json": bomb(64)})
        self.assertIn("metadata.json is larger", self.bounded(lambda: exports.verify_evidence_bytes(metadata_bomb, "job-1"))["error"])
        chain_bomb = deflated({"integrity.json": manifest("chain.json"), "chain.json": bomb(64)})
        self.assertIn("chain.json is larger", self.bounded(lambda: exports.verify_chain_member(zipfile.ZipFile(io.BytesIO(chain_bomb))))["error"])

    def test_a_case_document_is_bounded_too(self):
        upload = case_package([])
        with zipfile.ZipFile(io.BytesIO(upload)) as original:
            members = {name: original.read(name) for name in original.namelist()}
        members["case.json"] = bomb(64)
        crafted = deflated(members)
        result = self.bounded(lambda: cases.verify_package_bytes(crafted, "case-1", "package-1"))
        self.assertFalse(result["checksums_valid"])
        self.assertIn("is larger than a genuine package's", result["error"])

    def test_deeply_nested_json_is_an_invalid_package_not_a_server_error(self):
        nested = b"[" * 200_000 + b"]" * 200_000
        result = exports.verify_evidence_bytes(deflated({"integrity.json": nested}), "job-1")
        self.assertFalse(result["checksums_valid"])
        self.assertIn("nested too deeply", result["error"])

    def test_more_members_than_a_genuine_package_has_is_refused_before_the_directory_is_read(self):
        upload = deflated({f"file-{number}": b"" for number in range(package_checks.MAX_MEMBERS + 1)})
        with mock.patch.object(zipfile, "ZipFile", side_effect=AssertionError("the directory was read")):
            result = exports.verify_evidence_bytes(upload, "job-1")
        self.assertIn(f"more than {package_checks.MAX_MEMBERS} files", result["error"])

    def test_a_case_package_and_its_clips_share_one_inflation_budget(self):
        clips = []
        for number in range(2):
            path = Path(self.temporary.name) / f"clip-{number}.zip"
            evidence_package(path, 3 * MIB, export_id=f"clip-{number}")
            clips.append(path.read_bytes())
        upload = case_package(clips)
        with mock.patch.object(package_checks, "MAX_INFLATED_BYTES", len(upload) + 4 * MIB):  # Room for the package and one clip, not two
            result = cases.verify_package_bytes(upload, "case-1", "package-1")
        self.assertEqual([clip["valid"] for clip in result["clips"]], [True, False])
        self.assertIn("exceeds the verification size limit", result["clips"][1]["error"])
        self.assertTrue(cases.verify_package_bytes(upload, "case-1", "package-1")["clips_valid"])  # Within the real limit, the genuine package verifies

    def test_a_case_packages_clips_are_copied_to_the_recordings_volume_never_the_system_temporary_folder(self):
        path = Path(self.temporary.name) / "clip.zip"
        evidence_package(path, MIB, export_id="clip-0")
        folders = []
        real = tempfile.TemporaryFile

        def watched(*args, **kwargs):
            folders.append(kwargs.get("dir"))
            return real(*args, **kwargs)

        with mock.patch("app.cases.tempfile.TemporaryFile", watched):
            result = cases.verify_package_bytes(case_package([path.read_bytes()]), "case-1", "package-1")
        self.assertTrue(result["clips_valid"], result)
        self.assertEqual(folders, [package_checks.directory()])
        self.assertEqual(package_checks.directory().parent, recordings_path())


class UploadLimitTests(GrantCase):
    def completed_export(self) -> str:
        from app.store import connect
        with connect() as db:
            db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,requested_start,requested_end,status,created_at,updated_at) VALUES('job-1',1,?,?,?,'completed',?,?)",
                       (json.dumps([self.d1]), "2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
        return "job-1"

    def left(self) -> list[str]:
        folder = package_checks.directory()
        return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []

    def test_more_checks_at_once_than_allowed_are_refused_with_a_clear_message(self):
        job_id = self.completed_export()
        held = [package_checks.Slot().__enter__() for _ in range(package_checks.MAX_UPLOADS)]
        try:
            refused = self.admin.post(f"/api/exports/{job_id}/verify", content=b"anything", headers={"content-type": "application/zip"})
        finally:
            for slot in held:
                slot.__exit__(None, None, None)
        self.assertEqual(refused.status_code, 429)
        self.assertIn("package checks are running", refused.json()["detail"])
        self.assertEqual(refused.headers["Retry-After"], "30")
        self.assertEqual(self.left(), [])
        self.assertEqual(self.admin.post(f"/api/exports/{job_id}/verify", content=b"not a zip", headers={"content-type": "application/zip"}).status_code, 200)

    def test_an_upload_that_would_take_the_volume_below_its_headroom_is_refused_part_way_and_its_file_removed(self):
        """Driven directly with an upload that arrives in pieces: the test client hands the server a body all at once."""
        import asyncio

        from fastapi import HTTPException

        from app.api.export_jobs import received_package

        Usage = namedtuple("Usage", "total used free")
        written: list[int] = []

        def usage(folder):
            on_disk = sum(path.stat().st_size for path in Path(folder).iterdir()) if Path(folder).is_dir() else 0
            written.append(on_disk)
            return Usage(100 * MIB, 0, 10 * MIB - on_disk)  # The upload uses up what is free as it is written

        class Upload:
            headers: dict[str, str] = {}  # No length given

            async def stream(self):
                for _ in range(12):  # 6 MiB
                    yield b"x" * (512 * 1024)

        async def upload():
            async with received_package(Upload(), "too large"):
                self.fail("the whole upload was accepted")

        with mock.patch.object(package_checks.shutil, "disk_usage", usage), mock.patch("app.exports.free_floor", return_value=7 * MIB), \
                mock.patch.object(package_checks, "ROOM_CHECK_EVERY", MIB), self.assertRaises(HTTPException) as refused:
            asyncio.run(upload())
        self.assertEqual(refused.exception.status_code, 507)
        self.assertIn("not enough free space", refused.exception.detail)
        self.assertGreater(max(written), 0)  # It was refused part-way, with some of it written...
        self.assertLess(max(written), 4 * MIB)  # ...not after all of it
        self.assertEqual(self.left(), [])  # ...and that part removed
        self.assertEqual(package_checks._uploads, 0)  # Its place is given back

    def test_an_upload_whose_declared_length_would_not_fit_is_refused_before_anything_is_written(self):
        job_id = self.completed_export()
        with mock.patch("app.exports.free_floor", return_value=shutil.disk_usage(recordings_path()).free):
            refused = self.admin.post(f"/api/exports/{job_id}/verify", content=b"x" * 1024, headers={"content-type": "application/zip"})
        self.assertEqual(refused.status_code, 507)
        self.assertEqual(self.left(), [])


class LeftoverTests(GrantCase):
    def test_leftovers_are_removed_at_start_and_never_count_against_the_exports_budget(self):
        folder = package_checks.directory()
        folder.mkdir(parents=True)
        (folder / "upload-abc.zip").write_bytes(b"\0" * 300_000)
        exports_folder = recordings_path() / exports.EXPORT_DIRECTORY
        exports_folder.mkdir(parents=True, exist_ok=True)
        (exports_folder / ".verify-old.upload").write_bytes(b"\0" * 200_000)  # Where uploads went before
        (exports_folder / "kept.zip").write_bytes(b"\0" * 1000)
        self.assertEqual(copies_budget.usage()["exports"], 201_000)  # The current folder is not the exports'; the old leftover still counts...
        with self.assertLogs("app.package_checks", "WARNING") as logged:
            self.assertEqual(package_checks.remove_leftovers(), 500_000)
        self.assertIn("Removed 2 package upload(s)", logged.output[0])
        self.assertEqual(copies_budget.usage()["exports"], 1000)  # ...until it is removed at start
        self.assertTrue((exports_folder / "kept.zip").exists())
        self.assertEqual(list(folder.iterdir()), [])

    def test_the_supervisor_finds_the_same_folders_as_the_rest_of_the_nvr(self):
        self.assertEqual(package_checks._recordings_root(), recordings_path())
        self.assertEqual(package_checks.LEGACY_DIRECTORY, exports.EXPORT_DIRECTORY)


if __name__ == "__main__":
    unittest.main()
