"""A recordings disk that did not mount no longer makes the NVR forget the footage on it (#479).

The folder of a volume whose disk failed to mount is still there, empty, on the disk underneath. Each volume carries a marker file; a
volume with footage rows, no marker and none of the recorded files is "not mounted": nothing is recorded to it and nothing on it is removed."""
from __future__ import annotations

import json
import shutil
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import backup, headroom, tiering, volume_marker
from app.incidents import sync_storage_incidents
from app.main import app
from app.principal import Principal, administrator, current_user
from app.retention import cleanup_expired_recordings
from app.storage import camera_recordings_dir, storage_status
from app.store import connect
from tests.test_headroom import NOW, HeadroomCase


class MarkerCase(HeadroomCase):
    def setUp(self):
        super().setUp()
        volume_marker.forget()
        self.addCleanup(volume_marker.forget)

    def marker(self, volume: str = "default") -> dict | None:
        path = (self.root / "archive" if volume == "archive" else self.root) / volume_marker.MARKER_NAME
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def expected(self, volume: str = "default") -> str | None:
        with connect() as db:
            row = db.execute("SELECT marker_id FROM storage_volumes WHERE id=?", (volume,)).fetchone()
        return row[0] if row else None

    def disk_did_not_mount(self) -> None:
        """What is left when the host disk behind the recordings folder is absent: the folder, on the disk underneath, with nothing in it."""
        for item in self.root.iterdir():
            if item.name != "archive":
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        volume_marker.forget()

    def state(self, volume: str = "default") -> str:
        return next(item["state"] for item in storage_status()["volumes"] if item["id"] == volume)


class FirstUseTests(MarkerCase):
    def test_an_empty_volume_gets_its_marker_the_first_time_it_is_used(self):
        self.assertIsNone(self.marker())
        self.assertEqual(camera_recordings_dir(self.cameras["north"]), self.root / self.cameras["north"])
        self.assertEqual(self.marker()["id"], self.expected())
        self.assertTrue(self.expected())

    def test_an_installation_from_before_markers_gets_one_when_its_footage_is_there(self):
        self.segment("north", hours_ago=5)
        self.assertIsNone(self.marker())
        self.assertEqual(self.state(), "pressure")  # The test disk is tiny; what matters is that it is not "disk_not_mounted"
        self.assertEqual(self.marker()["id"], self.expected())

    def test_each_volume_has_its_own_marker(self):
        self.segment("north", hours_ago=5)
        self.segment("north", hours_ago=5, volume="archive")
        storage_status()
        self.assertTrue(self.marker() and self.marker("archive"))
        self.assertNotEqual(self.marker()["id"], self.marker("archive")["id"])

    def test_a_marker_deleted_by_hand_is_written_again_while_the_footage_is_there(self):
        self.segment("north", hours_ago=5)
        storage_status()
        first = self.expected()
        (self.root / volume_marker.MARKER_NAME).unlink()
        volume_marker.forget()
        self.assertEqual(self.state(), "pressure")  # The test disk is tiny; what matters is that it is not "disk_not_mounted"
        self.assertEqual(self.marker()["id"], first)


class DiskNotMountedTests(MarkerCase):
    ROWS = 300
    CAPACITY = 1000

    def setUp(self):
        super().setUp()
        self.kept = [self.segment("north", hours_ago=24 * 400 + index) for index in range(self.ROWS)]  # Far past any retention policy
        storage_status()  # The marker is on the disk
        self.assertTrue(self.marker())
        self.disk_did_not_mount()

    def rows(self) -> int:
        with connect() as db:
            return db.execute("SELECT COUNT(*) FROM recording_segments").fetchone()[0]

    def test_the_headroom_pass_removes_no_row_and_says_it_is_waiting(self):
        result = headroom.keep_headroom(NOW)  # Free space is far below the headroom: 300 units recorded on a 1000-unit disk reads as full
        self.assertEqual(result["default"], {"removed_segments": 0, "removed_bytes": 0, "short_bytes": 0, "waiting_for_storage": 1})
        self.assertEqual(self.rows(), self.ROWS)

    def test_retention_removes_no_row_and_says_it_is_waiting(self):
        with self.assertLogs("nvr.retention", level="WARNING"):
            result = cleanup_expired_recordings(NOW)
        self.assertEqual((result["removed_segments"], result["waiting_for_storage"]), (0, self.ROWS))
        self.assertEqual(self.rows(), self.ROWS)

    def test_the_recorder_is_refused_and_a_storage_incident_says_what_to_check(self):
        with self.assertRaisesRegex(OSError, "is not mounted there"):
            camera_recordings_dir(self.cameras["north"])
        self.assertEqual(list(self.root.glob(self.cameras["north"])), [], "nothing was made on the disk underneath")
        status = storage_status()
        self.assertEqual(self.state(), "disk_not_mounted")
        with connect() as db:
            sync_storage_incidents(db, status["volumes"])
            incident = db.execute("SELECT summary,fault_active FROM incidents WHERE incident_key='storage:default'").fetchone()
        self.assertEqual(incident["fault_active"], 1)
        self.assertIn("is not mounted", incident["summary"])
        self.assertIn("Settings > Recording & storage", incident["summary"])
        self.assertIsNone(self.marker(), "no marker is written onto the disk underneath")

    def test_tiering_leaves_the_volume_alone(self):
        with connect() as db:
            db.execute("INSERT INTO cold_tiers(id,name,kind,local_path) VALUES('cold','Cold','local','/nowhere')")
            db.execute("INSERT INTO tier_policies(volume_id,tier_id,after_days,enabled) VALUES('default','cold',1,1)")
        with patch.object(tiering, "candidates", side_effect=AssertionError("the volume's footage was looked at")), patch.object(tiering, "sweep_orphans"), patch.object(tiering, "sweep_switched"):
            self.assertEqual(tiering.run_once(NOW), {"waiting_for_storage": 1})

    def test_when_the_disk_is_back_everything_carries_on(self):
        for segment_id in self.kept[:3]:
            with connect() as db:
                relative = db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (segment_id,)).fetchone()[0]
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.root / relative).write_bytes(b"x")
        (self.root / volume_marker.MARKER_NAME).write_text(json.dumps({"id": self.expected()}), encoding="utf-8")
        volume_marker.forget()
        self.assertEqual(self.state(), "pressure")  # The test disk is tiny; what matters is that it is not "disk_not_mounted"
        result = cleanup_expired_recordings(NOW)
        self.assertEqual(result["removed_segments"], self.ROWS, "rows whose files are truly gone are cleaned up while the volume is present")

    def test_a_disk_from_another_nvr_is_not_used(self):
        (self.root / volume_marker.MARKER_NAME).write_text(json.dumps({"id": uuid.uuid4().hex}), encoding="utf-8")
        volume_marker.forget()
        self.assertEqual(self.state(), "wrong_disk")
        with self.assertRaisesRegex(OSError, "belongs to another NVR"):
            camera_recordings_dir(self.cameras["north"])
        self.assertEqual(headroom.keep_headroom(NOW)["default"]["waiting_for_storage"], 1)


class NewDiskTests(MarkerCase):
    def setUp(self):
        super().setUp()
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
        self.as_admin()
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app)
        self.default = [self.segment("north", hours_ago=index + 1) for index in range(4)]
        self.archive = [self.segment("south", hours_ago=index + 1, volume="archive") for index in range(2)]
        storage_status()

    def as_admin(self, token_id: str | None = None) -> None:
        principal = Principal(id=1, username="admin", role="administrator", token_id=token_id)
        app.dependency_overrides[administrator] = lambda: principal
        app.dependency_overrides[current_user] = lambda: principal

    def test_this_is_a_new_empty_disk_writes_a_marker_and_clears_the_old_disks_rows_only(self):
        old = self.expected()
        self.disk_did_not_mount()  # The old disk is gone; a new, empty one is in its place
        response = self.client.post("/api/storage/default/new-disk")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"volume_id": "default", "footage_rows_removed": 4})
        self.assertEqual(self.remaining_ids(), sorted(self.archive), "the other volume's footage is untouched")
        self.assertNotEqual(self.expected(), old)
        self.assertEqual(self.marker()["id"], self.expected())
        self.assertEqual(self.state(), "pressure")  # The test disk is tiny; what matters is that it is not "disk_not_mounted"
        self.assertEqual(camera_recordings_dir(self.cameras["north"]), self.root / self.cameras["north"])
        with connect() as db:
            audit = db.execute("SELECT actor_id,resource_id,details_json FROM audit_log WHERE action='storage_disk_replaced'").fetchone()
        self.assertEqual((audit["actor_id"], audit["resource_id"]), (1, "default"))
        self.assertEqual(json.loads(audit["details_json"]), {"was": "unmarked", "footage_rows_removed": 4})

    def remaining_ids(self) -> list[int]:
        with connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM recording_segments ORDER BY id")]

    def test_a_volume_that_is_the_right_disk_cannot_be_replaced_this_way(self):
        response = self.client.post("/api/storage/default/new-disk")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.remaining_ids()), 6)

    def test_an_api_token_cannot_do_it(self):
        self.disk_did_not_mount()
        self.as_admin(token_id="token-7")
        response = self.client.post("/api/storage/default/new-disk")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.remaining_ids()), 6)

    def test_an_unknown_volume_is_not_found(self):
        self.assertEqual(self.client.post("/api/storage/nowhere/new-disk").status_code, 404)


class RestoreTests(MarkerCase):
    def test_after_a_restore_the_disks_mounted_now_are_taken_and_missing_footage_is_marked(self):
        present = self.segment("north", hours_ago=2)
        gone = self.segment("north", hours_ago=3)
        storage_status()
        with connect() as db:
            relative = db.execute("SELECT relative_path FROM recording_segments WHERE id=?", (gone,)).fetchone()[0]
            db.execute("UPDATE storage_volumes SET marker_id='from-the-other-machine'")  # What the restored database remembers
        (self.root / relative).unlink()
        (self.root / volume_marker.MARKER_NAME).unlink()
        volume_marker.forget()
        backup.restore_marker().parent.mkdir(parents=True, exist_ok=True)
        backup.restore_marker().write_text("restored", encoding="utf-8")
        result = backup.reconcile_after_restore()
        self.assertEqual((result["checked"], result["marked_unavailable"], result["skipped_unavailable_storage"]), (2, 1, 0))
        self.assertEqual(self.marker()["id"], self.expected())
        with connect() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT id FROM recording_segments WHERE unavailable_at IS NULL")], [present])
        self.assertEqual(self.state(), "pressure")  # The test disk is tiny; what matters is that it is not "disk_not_mounted"


class FootageFilterTests(MarkerCase):
    def test_a_volumes_footage_is_what_is_under_its_folder_and_under_no_inner_volume(self):
        volumes = {"default": "", "archive": "archive"}
        default = self.segment("north", hours_ago=1)
        archive = self.segment("north", hours_ago=1, volume="archive")
        with connect() as db:
            for volume, segment_id in (("default", default), ("archive", archive)):
                where, values = volume_marker._prefix_filter(volume, volumes)
                self.assertEqual([row[0] for row in db.execute(f"SELECT id FROM recording_segments WHERE {where}", values)], [segment_id])

    def test_footage_that_moved_to_a_cold_tier_or_is_marked_unavailable_does_not_count(self):
        moved = self.segment("north", hours_ago=24 * 400)
        with connect() as db:
            db.execute("INSERT INTO cold_tiers(id,name,kind,local_path) VALUES('cold','Cold','local','/nowhere')")
            db.execute("UPDATE recording_segments SET tier_id='cold' WHERE id=?", (moved,))
        self.disk_did_not_mount()
        self.assertEqual(self.state(), "pressure", "nothing the database expects on this volume is missing")
        self.assertTrue(self.marker())
