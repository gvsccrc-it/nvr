"""Face-watch embeddings and pictures are encrypted at rest, and gone from the file once an entry is deleted (#520).

They were plain data in the database: readable in any copy of the file, and left in freed pages and the write-ahead log after a deletion,
because the database runs without ``secure_delete`` and is never vacuumed. As decided on the ticket: the three columns are encrypted with
the credential key, decrypted only for matching and the two picture routes, and deleting an entry overwrites what it frees and empties the
log. These tests read the raw files, as someone with a copy of them would."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import numpy as np

from app import credentials, face_models, face_scan, faces, migrations
from app.store import connect, database_path, initialize, shrink_log

AT = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
_ENV = ("NVR_DB_PATH", "NVR_RECORDINGS_PATH", "NVR_CREDENTIAL_KEY_PATH")


def distinctive_embedding(seed: int) -> np.ndarray:
    """A face's numbers no other bytes in the file will match by chance."""
    values = np.random.default_rng(seed).standard_normal(512).astype(np.float32)
    return values / np.linalg.norm(values)


def stored_bytes() -> bytes:
    """Everything a copy of the database's files would hold: the file and its write-ahead log."""
    path = database_path()
    return b"".join(candidate.read_bytes() for candidate in (path, path.with_name(path.name + "-wal")) if candidate.exists())


class AtRestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        root = Path(self.temporary.name)
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        initialize()
        self.camera = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES(?,?,X'01')", (self.camera, "Front gate"))
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES('t1',?,'person',?,?,'m','1','cpu')",
                       (self.camera, AT.isoformat(), AT.isoformat()))

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temporary.cleanup()

    def enrol(self, seed: int, picture: bytes) -> tuple[str, np.ndarray]:
        embedding = distinctive_embedding(seed)
        face = face_models.Face(0.95, (0.1, 0.1, 0.3, 0.4), ((0.1, 0.2),) * 5, embedding, (128, 192))
        with connect() as db:
            entry = faces.create_entry(db, f"Person {seed}", "Trespassed", None, picture, 1)
            faces.add_embedding(db, entry, face, self.camera, AT)
        return entry, embedding

    def sighting(self, entry: str, picture: bytes) -> str:
        sighting = str(uuid.uuid4())
        with connect() as db:
            db.execute("INSERT INTO face_sightings(id,entry_id,camera_id,track_id,seen_at,similarity,box_json,picture_ciphertext) VALUES(?,?,?,'t1',?,0.9,'{}',?)",
                       (sighting, entry, self.camera, AT.isoformat(), credentials.encrypt_bytes(picture)))
        return sighting


class EncryptedTests(AtRestCase):
    def test_a_raw_copy_holds_no_embedding_or_picture_and_matching_and_pictures_still_work(self):
        picture, seen = b"\xff\xd8ENROLMENT-PICTURE-" + os.urandom(64), b"\xff\xd8SIGHTING-PICTURE-" + os.urandom(64)
        entry, embedding = self.enrol(1, picture)
        self.sighting(entry, seen)
        shrink_log()
        raw = stored_bytes()
        self.assertNotIn(embedding.tobytes(), raw)
        self.assertNotIn(embedding.tobytes()[:64], raw)  # Not even a part of it
        self.assertNotIn(picture, raw)
        self.assertNotIn(seen, raw)
        with connect() as db:
            (watched_entry, (watched_embedding,)), = faces.watched(db)  # What matching compares with
            self.assertEqual(watched_entry, entry)
            np.testing.assert_allclose(watched_embedding, embedding.astype(np.float64), rtol=0, atol=0)
            self.assertEqual(faces.entry_picture(db, entry), picture)

    def test_the_sighting_kept_by_the_scanner_is_encrypted(self):
        entry, _ = self.enrol(2, b"\xff\xd8x")
        frame = np.full((480, 640, 3), 90, dtype=np.uint8)
        face = face_models.Face(0.95, (0.1, 0.1, 0.3, 0.4), ((0.1, 0.2),) * 5, distinctive_embedding(2), (128, 192))
        self.assertTrue(face_scan.keep_sighting(self.camera, "t1", entry, 0.9, face, frame, AT))
        with connect() as db:
            stored = db.execute("SELECT picture_ciphertext FROM face_sightings").fetchone()[0]
        self.assertTrue(bytes(stored).startswith(b"gAAAA"))
        self.assertEqual(credentials.decrypt_bytes(stored)[:2], b"\xff\xd8")

    def test_with_face_data_stored_a_missing_key_is_not_replaced(self):
        self.enrol(3, b"\xff\xd8x")
        credentials.key_path().unlink()
        credentials._loaded.clear()
        with self.assertLogs("nvr.credentials", "ERROR"), self.assertRaises(credentials.CredentialKeyMissing):
            credentials.ensure_key()
        self.assertFalse(credentials.key_path().exists())

    def test_loading_the_watch_list_stays_quick(self):
        """Matching compares numbers already in memory, as before; what encryption adds is decrypting them when the list is loaded."""
        for seed in range(100):
            entry, _ = self.enrol(1000 + seed, b"\xff\xd8x")
            face = face_models.Face(0.95, (0.1, 0.1, 0.3, 0.4), ((0.1, 0.2),) * 5, distinctive_embedding(5000 + seed), (128, 192))
            with connect() as db:
                for _ in range(4):
                    faces.add_embedding(db, entry, face, self.camera, AT)
        with connect() as db:
            faces.watched(db)
            started = time.perf_counter()
            loaded = faces.watched(db)
            took = time.perf_counter() - started
        self.assertEqual(sum(len(embeddings) for _, embeddings in loaded), 500)
        self.assertLess(took, 1.0, f"500 embeddings took {took:.3f} s to load")


class ErasedTests(AtRestCase):
    def test_after_an_entry_is_deleted_nothing_of_it_is_left_in_the_file_or_its_log(self):
        entry, _ = self.enrol(4, b"\xff\xd8PICTURE")
        self.sighting(entry, b"\xff\xd8SEEN")
        with connect() as db:
            tokens = [bytes(row[0]) for row in db.execute("SELECT embedding_ciphertext FROM face_embeddings UNION ALL SELECT picture_ciphertext FROM face_watch_entries "
                                                          "UNION ALL SELECT picture_ciphertext FROM face_sightings")]
        self.assertEqual(len(tokens), 3)
        shrink_log()
        self.assertTrue(all(token in stored_bytes() for token in tokens))
        with connect() as db:
            gone = faces.get_entry(db, entry)
        faces.erase_entry(entry, 1, gone)
        raw = stored_bytes()
        for token in tokens:
            self.assertNotIn(token, raw)
            self.assertNotIn(token[:40], raw)
        with connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM face_sightings").fetchone()[0], 0)
            (removed,) = db.execute("SELECT details_json FROM audit_log WHERE action='face_watch_removed'").fetchall()
        self.assertIn("Person 4", removed[0])  # The security log keeps the record that a named person was removed, as the docs say

    def test_without_secure_delete_the_bytes_would_have_stayed(self):
        """What the ticket found, so the test above can be trusted to notice."""
        entry, _ = self.enrol(5, b"\xff\xd8PICTURE")
        with connect() as db:
            token = bytes(db.execute("SELECT embedding_ciphertext FROM face_embeddings").fetchone()[0])
        shrink_log()
        with connect() as db:
            db.execute("PRAGMA secure_delete = OFF")
            db.execute("DELETE FROM face_watch_entries WHERE id=?", (entry,))
        shrink_log()
        self.assertIn(token, stored_bytes())

    def test_the_delete_route_erases(self):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.principal import current_user
        from tests.test_support import principal

        entry, _ = self.enrol(6, b"\xff\xd8PICTURE")
        app.dependency_overrides[current_user] = lambda: principal(1, "admin", "administrator")
        try:
            with mock.patch.object(faces, "erase_entry", wraps=faces.erase_entry) as erase, TestClient(app) as client:
                self.assertEqual(client.delete(f"/api/faces/watch/{entry}").status_code, 204)
        finally:
            app.dependency_overrides.pop(current_user, None)
        erase.assert_called_once()
        with connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM face_watch_entries WHERE id=?", (entry,)).fetchone())


class MigrationTests(AtRestCase):
    def setUp(self):  # A database as it was before #520, with face data in the clear
        self.temporary = tempfile.TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in _ENV}
        root = Path(self.temporary.name)
        os.environ["NVR_DB_PATH"] = str(root / "state.sqlite3")
        os.environ["NVR_RECORDINGS_PATH"] = str(root / "recordings")
        os.environ["NVR_CREDENTIAL_KEY_PATH"] = str(root / "credentials.key")
        before = [(version, module) for version, module in migrations.MIGRATIONS if version < 103]
        with mock.patch.object(migrations, "MIGRATIONS", before):
            initialize()
        self.embedding = distinctive_embedding(7)
        self.picture, self.seen = b"\xff\xd8OLD-ENROLMENT-" + os.urandom(64), b"\xff\xd8OLD-SIGHTING-" + os.urandom(64)
        with connect() as db:
            db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(1,'admin','unused','administrator')")
            db.execute("INSERT INTO cameras(id,name,stream_url_ciphertext) VALUES('c1','Front gate',X'01')")
            db.execute("INSERT INTO ai_tracks(id,camera_id,label,first_seen_at,last_seen_at,model_id,model_version,provider) VALUES('t1','c1','person',?,?,'m','1','cpu')",
                       (AT.isoformat(), AT.isoformat()))
            db.execute("INSERT INTO face_watch_entries(id,name,reason,picture) VALUES('e1','Person','Trespassed',?)", (self.picture,))
            db.execute("INSERT INTO face_embeddings(entry_id,embedding) VALUES('e1',?)", (self.embedding.tobytes(),))
            db.execute("INSERT INTO face_sightings(id,entry_id,camera_id,track_id,seen_at,similarity,box_json,picture) VALUES('s1','e1','c1','t1',?,0.9,'{}',?)",
                       (AT.isoformat(), self.seen))
        shrink_log()

    def test_the_upgrade_encrypts_what_was_stored_and_leaves_no_plain_copy_in_the_file(self):
        self.assertIn(self.embedding.tobytes(), stored_bytes())
        self.assertIn(103, migrations.migrate(database_path(), connect))  # With any later steps
        shrink_log()
        raw = stored_bytes()
        for plain in (self.embedding.tobytes(), self.picture, self.seen):
            self.assertNotIn(plain, raw)
        with connect() as db:
            self.assertEqual(faces.entry_picture(db, "e1"), self.picture)
            ((entry, (embedding,)),) = faces.watched(db)
            np.testing.assert_array_equal(embedding, self.embedding.astype(np.float64))
            self.assertEqual(credentials.decrypt_bytes(db.execute("SELECT picture_ciphertext FROM face_sightings").fetchone()[0]), self.seen)
        self.assertTrue(credentials._stored_secrets_exist())  # The key that encrypted them is now one that must not be replaced
        # The copy made before the upgrade still holds them in the clear, as docs/face-watch.md says
        (copy,) = database_path().parent.glob(f"{database_path().name}.pre-migration-*")
        self.assertIn(self.embedding.tobytes(), copy.read_bytes())

    def test_an_installation_without_face_data_needs_no_key_for_the_upgrade(self):
        with connect() as db:
            for table in ("face_sightings", "face_embeddings", "face_watch_entries"):
                db.execute(f"DELETE FROM {table}")
        with mock.patch.object(credentials, "encrypt_bytes", side_effect=AssertionError("the key was used")):
            self.assertIn(103, migrations.migrate(database_path(), connect))  # With any later steps
        self.assertFalse(credentials.key_path().exists())


if __name__ == "__main__":
    unittest.main()
