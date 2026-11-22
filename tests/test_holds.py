from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from fastapi import HTTPException

from app.main import app
from app.principal import administrator, current_user
from app.retention import cleanup_expired_recordings
from app.store import connect, initialize


class PreservationHoldTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.previous={key:os.environ.get(key) for key in ("NVR_DB_PATH","NVR_RECORDINGS_PATH","NVR_STORAGE_VOLUMES")}
        self.root=Path(self.temporary.name)/"recordings"
        self.root.mkdir()
        os.environ["NVR_DB_PATH"]=str(Path(self.temporary.name)/"state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"]=str(self.root)
        os.environ.pop("NVR_STORAGE_VOLUMES",None)
        initialize()
        self.camera=str(uuid.uuid4());self.segment_id=None
        with connect() as db:
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('operator','unused','operator')")
            self.operator_id=db.execute("SELECT id FROM users WHERE username='operator'").fetchone()[0]
            db.execute("INSERT INTO users(username,password_hash,role) VALUES('administrator','unused','administrator')")
            self.admin_id=db.execute("SELECT id FROM users WHERE username='administrator'").fetchone()[0]
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext,recording_mode) VALUES(?,?,X'01','disabled')",(self.camera,"Held camera"))
            started=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat();ended=(datetime.now(timezone.utc)-timedelta(days=10,minutes=-1)).isoformat()
            db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,?)",(self.camera,f"{self.camera}/segment.mp4",started,ended,256))
            self.segment_id=db.execute("SELECT id FROM recording_segments WHERE camera_id=?",(self.camera,)).fetchone()[0]
        self.media=self.root/self.camera/"segment.mp4";self.media.parent.mkdir();self.media.write_bytes(b"footage"*100)
        self.active_user={"id":self.operator_id,"role":"operator"}
        app.dependency_overrides[current_user]=lambda:self.active_user
        def admin_override():
            if self.active_user["role"]!="administrator": raise HTTPException(status_code=403,detail="administrator role required")
            return self.active_user
        app.dependency_overrides[administrator]=admin_override
        self.context=TestClient(app);self.client=self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None,None,None)
        app.dependency_overrides.pop(current_user,None);app.dependency_overrides.pop(administrator,None)
        for key,value in self.previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
        self.temporary.cleanup()

    def test_operator_hold_blocks_cleanup_and_delete_until_admin_release_then_audits(self):
        url=f"/api/cameras/{self.camera}/segments/{self.segment_id}"
        with connect() as db:
            self.assertIsNotNone(
                db.execute("SELECT 1 FROM recording_segments WHERE id=? AND camera_id=?", (self.segment_id, self.camera)).fetchone(),
                "the test interval must still exist after application startup",
            )
        denied=self.client.post(url+"/hold",json={"expires_in_days":3})
        self.assertEqual(denied.status_code,403)
        held=self.client.post(url+"/hold",json={})
        self.assertEqual(held.status_code,201,held.text)
        # An operator's hold used to have no expiry. By the owner's decision on #491 it now runs for 30 days.
        expires=datetime.fromisoformat(held.json()["expires_at"])
        self.assertAlmostEqual((expires-datetime.now(timezone.utc)).total_seconds(),30*86400,delta=120)
        with connect() as db:
            db.execute("INSERT INTO retention_policy_overrides(scope_type,scope_id,retention_days) VALUES('camera',?,1)",(self.camera,))
        self.assertEqual(self.client.delete(url).status_code,403)
        self.assertEqual(cleanup_expired_recordings()["removed_segments"],0)
        self.assertTrue(self.media.exists())
        self.active_user={"id":self.admin_id,"role":"administrator"}
        self.assertEqual(self.client.delete(url).status_code,409)
        released=self.client.delete(url+"/hold")
        self.assertEqual(released.status_code,204)
        expiring=self.client.post(url+"/hold",json={"expires_in_days":2})
        self.assertEqual(expiring.status_code,201,expiring.text)
        listed=self.client.get(f"/api/cameras/{self.camera}/segments").json()["segments"][0]
        self.assertTrue(listed["preserved"])
        self.assertIsNotNone(listed["hold_expires_at"])
        self.assertEqual(self.client.delete(url).status_code,409)
        self.assertEqual(self.client.delete(url+"/hold").status_code,204)
        self.assertEqual(self.client.delete(url).status_code,204)
        self.assertFalse(self.media.exists())
        with connect() as db:
            actions=[row[0] for row in db.execute("SELECT action FROM audit_log ORDER BY id")]
        self.assertEqual(actions,["preserve","release_hold","preserve","release_hold","delete"])



    def hold_rows(self):
        with connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM recording_holds ORDER BY created_at")]

    def audit(self, action: str):
        import json
        with connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT details_json FROM audit_log WHERE action=? ORDER BY id",(action,))]

    def test_an_administrator_extends_an_operators_hold_or_makes_it_indefinite_and_each_step_is_logged(self):
        url=f"/api/cameras/{self.camera}/segments/{self.segment_id}/hold"
        self.assertEqual(self.client.post(url,json={}).status_code,201)
        self.assertEqual(self.client.patch(url,json={"expires_in_days":90}).status_code,403)  # Not an operator's to change
        self.active_user={"id":self.admin_id,"role":"administrator"}
        longer=self.client.patch(url,json={"expires_in_days":90})
        self.assertEqual(longer.status_code,200,longer.text)
        self.assertAlmostEqual((datetime.fromisoformat(longer.json()["expires_at"])-datetime.now(timezone.utc)).total_seconds(),90*86400,delta=120)
        kept=self.client.patch(url,json={"expires_in_days":None})
        self.assertEqual((kept.status_code,kept.json()["expires_at"]),(200,None))  # Until released
        self.assertIsNone(self.hold_rows()[0]["expires_at"])
        self.assertEqual(self.client.delete(url).status_code,204)
        self.assertEqual(self.client.patch(url,json={"expires_in_days":5}).status_code,404)  # Nothing held any more
        # Placing, extending and releasing are each in the security log.
        self.assertEqual((len(self.audit("preserve")),len(self.audit("extend_hold")),len(self.audit("release_hold"))),(1,2,1))
        self.assertEqual(self.audit("extend_hold")[1]["expires_at"],None)
        # An administrator's own hold is as they set it: no expiry unless they give one.
        own=self.client.post(url,json={})
        self.assertEqual((own.status_code,own.json()["expires_at"]),(201,None))

    def test_one_operators_holds_together_may_cover_a_tenth_of_the_volume_and_no_more(self):
        from collections import namedtuple
        from unittest.mock import patch

        from app import preservation

        Usage=namedtuple("Usage","total used free")
        with connect() as db:  # Ten more intervals of 256 bytes each, on a "disk" of 10,000: a tenth is 1,000
            for index in range(10):
                name=f"{self.camera}/more-{index}.mp4"
                (self.root/name).write_bytes(b"footage")
                db.execute("INSERT INTO recording_segments(camera_id,relative_path,starts_at,ends_at,size_bytes) VALUES(?,?,?,?,256)",
                           (self.camera,name,f"2026-11-01T10:{index:02d}:00+00:00",f"2026-11-01T10:{index:02d}:59+00:00"))
            segments=[row[0] for row in db.execute("SELECT id FROM recording_segments WHERE camera_id=? ORDER BY id",(self.camera,))]
        def hold(segment):
            return self.client.post(f"/api/cameras/{self.camera}/segments/{segment}/hold",json={})
        with patch("shutil.disk_usage",lambda path:Usage(10_000,0,10_000)):
            self.assertEqual([hold(segment).status_code for segment in segments[:3]],[201,201,201])  # 768 bytes held
            refused=hold(segments[3])  # 1,024 would pass the tenth
            self.assertEqual(refused.status_code,409)
            self.assertIn("at most 10%",refused.json()["detail"])
            self.assertIn("Ask an administrator",refused.json()["detail"])
            with connect() as db:
                self.assertEqual(preservation.operator_held_bytes(db,self.operator_id,"default",datetime.now(timezone.utc)),768)
            # Another operator has their own tenth, and an administrator has no limit.
            with connect() as db:
                db.execute("INSERT INTO users(username,password_hash,role) VALUES('second','unused','operator')")
                second=db.execute("SELECT id FROM users WHERE username='second'").fetchone()[0]
            self.active_user={"id":second,"role":"operator"}
            self.assertEqual(hold(segments[3]).status_code,201)
            self.active_user={"id":self.admin_id,"role":"administrator"}
            self.assertEqual([hold(segment).status_code for segment in segments[4:9]],[201]*5)
            # A hold released, or one that belongs to a case, does not count against the operator.
            self.assertEqual(self.client.delete(f"/api/cameras/{self.camera}/segments/{segments[0]}/hold").status_code,204)
            with connect() as db:
                db.execute("UPDATE recording_holds SET case_id='a-case' WHERE segment_id=?",(segments[1],))
            self.active_user={"id":self.operator_id,"role":"operator"}
            self.assertEqual(hold(segments[9]).status_code,201)


if __name__=="__main__":
    unittest.main()
