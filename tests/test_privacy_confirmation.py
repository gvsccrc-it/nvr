"""A camera that loses its own privacy masks, or is replaced at the same address, is masked by the NVR again (#468).

By the decision on the ticket: the camera is asked for its masks on every health pass (10 minutes), when its stream reconnects, and when its
source or login is saved. If they are missing or different, or it could not be asked for 15 minutes, the NVR draws them itself from that
moment and raises an incident, and footage recorded while they were unconfirmed is masked on the way out. "Always mask on the NVR as well"
is a per-camera switch, off by default."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from app import privacy, privacy_check
from app.clock import now as clock_now
from app.credentials import encrypt
from app.store import connect
from tests.fixtures.mask_device import MaskDevice
from tests.test_privacy import MASK, PrivacyCase, brightness
from tests.test_support import principal


class ConfirmationCase(PrivacyCase):
    def setUp(self):
        super().setUp()
        self.device = MaskDevice()
        self.addCleanup(self.device.close)
        with connect() as db:
            db.execute("UPDATE cameras SET device_service_url=?,motion_credentials_ciphertext=? WHERE id=?",
                       (self.device.url, encrypt(json.dumps({"username": "onvif", "password": "pw"})), self.camera_id))
        self.status = self.mask()
        self.assertEqual(self.status["camera_side"], "applied")
        self.since = clock_now()

    def zones(self, recorded_at=None) -> list:
        with connect() as db:
            return privacy.server_zones(db, self.camera_id, recorded_at)

    def check(self, now=None) -> str:
        result = asyncio.run(privacy_check.ask(self.camera_id))
        privacy_check.record(self.camera_id, result, now)
        return result

    def incident(self) -> dict | None:
        with connect() as db:
            row = db.execute("SELECT state,fault_active FROM incidents WHERE incident_key=?", (f"privacy_masks:{self.camera_id}",)).fetchone()
        return dict(row) if row else None

    def status_now(self) -> dict:
        return self.client.get(f"/api/cameras/{self.camera_id}/privacy").json()


class MissingMasksTests(ConfirmationCase):
    def test_masks_removed_on_the_camera_are_drawn_again_and_an_incident_is_raised(self):
        self.assertEqual(self.check(), "confirmed")
        self.assertEqual(self.zones(), [])
        self.assertIsNone(self.incident())
        self.device.masks.clear()  # A factory reset, or someone deleting them on the camera
        self.assertEqual(self.check(), "missing")
        self.assertEqual(self.zones(), [MASK])  # Live
        self.assertEqual(self.incident(), {"state": "active", "fault_active": 1})
        shown = self.status_now()
        self.assertTrue(shown["server_enforced"])
        self.assertIsNotNone(shown["unconfirmed_since"])
        # Footage recorded after the last confirmation is masked on the way out
        recorded = self.since + timedelta(minutes=5)
        self.assertEqual(self.zones(recorded), [MASK])

    def test_a_mask_switched_off_or_changed_on_the_camera_counts_as_missing(self):
        token = next(iter(self.device.masks))
        self.device.disabled.add(token)
        self.assertEqual(self.check(), "missing")
        self.device.disabled.clear()
        self.device.masks[token] = [(-0.5, 0.5), (0.0, 0.5), (0.0, 0.0), (-0.5, 0.0)]  # Moved
        privacy_check.record(self.camera_id, "confirmed")  # (as if confirmed in between)
        self.assertEqual(self.check(), "missing")

    def test_a_different_camera_at_the_same_address_is_not_trusted(self):
        self.device.lists_masks = False  # Answers at the address, but has none of the NVR's masks to list
        self.assertEqual(self.check(), "missing")
        self.assertEqual(self.zones(), [MASK])

    def test_confirmed_again_the_camera_is_trusted_and_the_gap_stays_masked(self):
        kept = dict(self.device.masks)
        self.device.masks.clear()
        lost_at = clock_now()
        self.check()
        self.device.masks.update(kept)  # Someone put them back on the camera
        self.assertEqual(self.check(now=lost_at + timedelta(minutes=10)), "confirmed")
        self.assertEqual(self.zones(), [])
        self.assertEqual(self.incident()["state"], "recovered")
        self.assertIsNone(self.status_now()["unconfirmed_since"])
        self.assertEqual(self.zones(lost_at + timedelta(minutes=1)), [MASK])  # Recorded in the gap: still masked on the way out
        self.assertEqual(self.zones(lost_at + timedelta(minutes=12)), [])  # After it closed: the camera's own masks

    def test_playback_of_footage_from_the_gap_is_masked(self):
        since = datetime_of(self.status["camera_masked_since"])
        newer = self.segment_at(since + timedelta(seconds=60))
        self.assertGreater(brightness(self.client.get(newer).content)[0], 200)  # The camera's own masks: served as recorded
        self.device.masks.clear()
        self.check()
        self.assertLess(brightness(self.client.get(newer).content)[0], 30)

    def segment_at(self, starts):
        import shutil
        name = f"at-{int(starts.timestamp())}.mp4"
        shutil.copy(self.clip, self.clip.with_name(name))
        with connect() as db:
            segment = db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",
                                 (self.camera_id, f"{self.camera_id}/{name}", starts.isoformat(), (starts + timedelta(seconds=2)).isoformat(),
                                  self.clip.stat().st_size)).lastrowid
        return f"/api/cameras/{self.camera_id}/segments/{segment}/media"


class UnreachableTests(ConfirmationCase):
    def test_a_camera_that_cannot_be_asked_counts_as_unconfirmed_after_fifteen_minutes(self):
        self.assertEqual(self.check(), "confirmed")
        self.device.answering = False
        first = clock_now()
        self.assertEqual(self.check(now=first), "unreachable")
        self.assertEqual(self.zones(), [])  # Not yet: a camera restarting is not a lost mask
        self.assertIsNone(self.incident())
        self.assertEqual(self.check(now=first + timedelta(minutes=14)), "unreachable")
        self.assertEqual(self.zones(), [])
        self.assertEqual(self.check(now=first + timedelta(minutes=15)), "unreachable")
        self.assertEqual(self.zones(), [MASK])
        self.assertEqual(self.incident()["state"], "active")

    def test_while_unreachable_it_is_asked_sooner_so_the_limit_holds(self):
        self.device.answering = False
        first = clock_now()
        self.check(now=first)
        with connect() as db:
            self.assertEqual(privacy_check.due(db, first + privacy_check.RECHECK_UNREACHABLE + timedelta(seconds=1)), [self.camera_id])
            self.assertEqual(privacy_check.due(db, first + timedelta(minutes=1)), [])
        self.assertLess(privacy_check.RECHECK_UNREACHABLE * 2, privacy_check.UNREACHABLE_LIMIT)


class WhenAskedTests(ConfirmationCase):
    def test_due_every_ten_minutes_and_at_once_when_requested(self):
        now = clock_now()
        self.check(now=now)
        with connect() as db:
            self.assertEqual(privacy_check.due(db, now + timedelta(minutes=9)), [])
            self.assertEqual(privacy_check.due(db, now + timedelta(minutes=10, seconds=1)), [self.camera_id])
            privacy_check.request_check(db, self.camera_id)  # The stream reconnected, or the source or login was saved
            self.assertEqual(privacy_check.due(db, now + timedelta(seconds=1)), [self.camera_id])

    def test_a_reconnect_requests_a_check(self):
        from app.recording import RecordingManager

        recorder = RecordingManager.__new__(RecordingManager)
        recorder._status_written = {}
        self.check()
        recorder._set_status(self.camera_id, "recording")
        with connect() as db:
            self.assertEqual(db.execute("SELECT check_requested FROM privacy_masks WHERE camera_id=?", (self.camera_id,)).fetchone()[0], 0)
        recorder._set_status(self.camera_id, "reconnecting", "Camera stream interrupted; retrying")
        recorder._set_status(self.camera_id, "recording")
        with connect() as db:
            self.assertEqual(db.execute("SELECT check_requested FROM privacy_masks WHERE camera_id=?", (self.camera_id,)).fetchone()[0], 1)

    def test_an_idle_pass_writes_nothing(self):
        now = clock_now()
        self.check(now=now)
        with connect() as db:
            db.set_trace_callback(None)
        statements = []

        def watch(db):
            db.set_trace_callback(statements.append)
        from app import store
        original = store.connect

        def connect_watched(*args, **kwargs):
            connection = original(*args, **kwargs)
            watch(connection)
            return connection
        from unittest.mock import patch
        with patch("app.privacy_check.connect", connect_watched):
            self.assertEqual(asyncio.run(privacy_check.check_due(now + timedelta(minutes=1))), 0)
        self.assertFalse([statement for statement in statements if statement.split()[0].upper() in {"BEGIN", "UPDATE", "INSERT", "DELETE"}])

    def test_the_health_pass_asks_and_a_live_stream_restarts_when_trust_changes(self):
        self.check()
        guard = privacy.StreamGuard(principal(1, "viewer", "administrator"), self.camera_id, False)
        guard._access_checked = 0
        self.assertTrue(guard.still_valid())
        self.device.masks.clear()
        with connect() as db:
            privacy_check.request_check(db, self.camera_id)
        self.assertEqual(asyncio.run(privacy_check.check_due()), 1)
        guard._access_checked = 0
        self.assertFalse(guard.still_valid())
        self.assertEqual(guard.reason, "masks")


    def test_a_change_before_a_new_streams_first_check_still_restarts_it(self):
        self.check()
        started = privacy.trust_version(self.camera_id)  # Read where the stream's masking is decided, as the live routes do
        guard = privacy.StreamGuard(principal(1, "viewer", "administrator"), self.camera_id, False, trust_version=started)
        self.device.masks.clear()
        self.check()  # Lost before the stream's first check
        guard._access_checked = 0
        self.assertFalse(guard.still_valid())
        self.assertEqual(guard.reason, "masks")


class AlwaysServerTests(ConfirmationCase):
    def test_off_by_default_and_when_on_the_nvr_masks_even_a_confirmed_camera(self):
        self.check()
        self.assertFalse(self.status_now()["always_server"])
        self.assertEqual(self.zones(), [])
        answer = self.client.put(f"/api/cameras/{self.camera_id}/privacy/always-server", json={"enabled": True})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertTrue(answer.json()["always_server"])
        self.assertEqual(self.zones(), [MASK])
        self.assertEqual(self.zones(self.since + timedelta(minutes=5)), [MASK])
        self.client.put(f"/api/cameras/{self.camera_id}/privacy/always-server", json={"enabled": False})
        self.assertEqual(self.zones(), [])
        with connect() as db:
            audited = [json.loads(row[0])["enabled"] for row in db.execute(
                "SELECT details_json FROM audit_log WHERE action='privacy_masks_always_server_changed' ORDER BY id")]
        self.assertEqual(audited, [True, False])

    def test_only_an_administrator_may_change_it(self):
        refused = self.operator().put(f"/api/cameras/{self.camera_id}/privacy/always-server", json={"enabled": True})
        self.assertEqual(refused.status_code, 403)

    def test_the_privacy_page_says_when_the_masks_were_last_confirmed(self):
        self.check()
        shown = self.status_now()
        self.assertIsNotNone(shown["confirmed_at"])
        self.assertIsNone(shown["unconfirmed_since"])


def datetime_of(text):
    from datetime import datetime
    return datetime.fromisoformat(text)


if __name__ == "__main__":
    import unittest

    unittest.main()
