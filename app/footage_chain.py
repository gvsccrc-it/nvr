"""Signed footage: per-camera hash chains over recorded segments, signatures and verification.

Each link holds a segment's SHA-256, its time range and size, and the previous link's
hash; its own hash covers all of that. A camera's head is signed with the installation's
Ed25519 key, so a chain cannot be rewritten without the key. Verification recomputes the
chain, checks the latest signature over it, and compares every segment still on disk with
its link. Removals are links too, so retention and deletion are expected, not tampering.

The chain is appended by the background worker, off the recorder's write path.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .credentials import decrypt, encrypt
from .store import connect

GENESIS = "0" * 64
HASH_CHUNK = 1024 * 1024
SIGN_INTERVAL_SECONDS = 60
BATCH = 50
UNREADABLE_RETRY_SECONDS = 600
# A link older than this without a signature, or footage older than this never hashed, is a problem rather than work in progress.
OVERDUE = timedelta(minutes=10)
PROBLEMS = frozenset({"altered", "missing", "out_of_order", "signature_missing"})

logger = logging.getLogger(__name__)
# Segments whose file could not be read, with when they were last tried: skipped for a while so they never hold back newer footage.
_unreadable: dict[int, float] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def link_hash(link: dict[str, Any]) -> str:
    """The hash of one link: its fields and the previous link's hash, in a fixed form."""
    canonical = json.dumps({key: link[key] for key in ("camera_id", "seq", "kind", "segment_id", "relative_path", "starts_at", "ends_at",
                                                        "size_bytes", "sha256", "prev_hash")}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def signed_message(camera_id: str, upto_seq: int, head_hash: str) -> bytes:
    return f"nvr-footage-chain:v1:{camera_id}:{upto_seq}:{head_hash}".encode("utf-8")


def _key(db: sqlite3.Connection) -> tuple[Ed25519PrivateKey, str]:
    row = db.execute("SELECT private_key_ciphertext,public_key FROM footage_signing_key WHERE singleton_id=1").fetchone()
    if row is None:
        private = Ed25519PrivateKey.generate()
        raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        db.execute("INSERT OR IGNORE INTO footage_signing_key(singleton_id,private_key_ciphertext,public_key) VALUES(1,?,?)",
                   (encrypt(base64.b64encode(raw).decode()), public))
        row = db.execute("SELECT private_key_ciphertext,public_key FROM footage_signing_key WHERE singleton_id=1").fetchone()
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(decrypt(row["private_key_ciphertext"])))
    return private, row["public_key"]


def public_key() -> str:
    with connect() as db:
        return _key(db)[1]


def _head(db: sqlite3.Connection, camera_id: str) -> tuple[int, str]:
    row = db.execute("SELECT seq,link_hash FROM footage_chain WHERE camera_id=? ORDER BY seq DESC LIMIT 1", (camera_id,)).fetchone()
    return (int(row["seq"]), row["link_hash"]) if row else (0, GENESIS)


def _heads(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """Each camera's newest link number, stepping from camera to camera through the index: a row read per camera, not per link."""
    return db.execute(
        "WITH RECURSIVE c(camera_id) AS (SELECT MIN(camera_id) FROM footage_chain UNION ALL "
        "SELECT (SELECT MIN(camera_id) FROM footage_chain WHERE camera_id>c.camera_id) FROM c WHERE c.camera_id IS NOT NULL) "
        "SELECT camera_id,(SELECT MAX(seq) FROM footage_chain f WHERE f.camera_id=c.camera_id) AS seq FROM c WHERE camera_id IS NOT NULL").fetchall()


def _signature_at_or_after(db: sqlite3.Connection, camera_id: str, seq: int) -> sqlite3.Row | None:
    """The first signature that covers link ``seq``."""
    return db.execute("SELECT * FROM footage_chain_signatures WHERE camera_id=? AND upto_seq>=? ORDER BY upto_seq LIMIT 1", (camera_id, seq)).fetchone()


def _signature_before(db: sqlite3.Connection, camera_id: str, seq: int) -> sqlite3.Row | None:
    """The last signature made before link ``seq`` existed: a signed point to start checking from."""
    return db.execute("SELECT * FROM footage_chain_signatures WHERE camera_id=? AND upto_seq<? ORDER BY upto_seq DESC LIMIT 1", (camera_id, seq)).fetchone()


def _latest_signature(db: sqlite3.Connection, camera_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM footage_chain_signatures WHERE camera_id=? ORDER BY upto_seq DESC LIMIT 1", (camera_id,)).fetchone()


def _append(db: sqlite3.Connection, link: dict[str, Any]) -> None:
    seq, previous = _head(db, link["camera_id"])
    link = {**link, "seq": seq + 1, "prev_hash": previous}
    link["link_hash"] = link_hash(link)
    db.execute("INSERT INTO footage_chain(camera_id,seq,kind,segment_id,relative_path,starts_at,ends_at,size_bytes,sha256,prev_hash,link_hash,created_at) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (link["camera_id"], link["seq"], link["kind"], link["segment_id"], link["relative_path"], link["starts_at"], link["ends_at"],
                link["size_bytes"], link["sha256"], link["prev_hash"], link["link_hash"], _now()))


# Every segment up to this id (per database file) was found chained the last time it was looked at, so a pass looks only at newer ones.
# The check used to read every segment the camera ever recorded, every five seconds and for every export: 0.37 s a time for sixteen
# cameras and thirty days, with nothing new (#352). A segment below the mark can still need a link later (its storage came back, or its
# file became readable), so the whole table is looked at again every FULL_SCAN_SECONDS.
FULL_SCAN_SECONDS = 600.0
_looked: dict[str, tuple[int, float]] = {}  # Database file -> (the mark, when the whole table was last looked at)


def _removals_waiting() -> bool:
    from .store import has_row

    with connect() as db:
        return has_row(db, "SELECT 1 FROM footage_removals LIMIT 1")


def _unchained(db: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    now = time.monotonic()
    for segment_id, tried in list(_unreadable.items()):
        if now - tried >= UNREADABLE_RETRY_SECONDS:
            del _unreadable[segment_id]
    skipped = json.dumps(sorted(_unreadable))
    database = str(db.execute("PRAGMA database_list").fetchone()[2])
    mark, scanned = _looked.get(database, (0, None))
    whole = scanned is None or now - scanned >= FULL_SCAN_SECONDS
    # Read before the look, so a segment indexed while it runs is not counted as looked at.
    newest = int(db.execute("SELECT COALESCE(MAX(id),0) FROM recording_segments").fetchone()[0])
    start = db.execute("SELECT after_segment_id FROM footage_chain_start").fetchone()
    after = max(int(start[0]) if start else 0, 0 if whole else mark)  # One plain bound, so the look begins there instead of reading up to it
    rows = db.execute(
        "SELECT s.* FROM recording_segments s WHERE s.id>? AND s.unavailable_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM footage_chain f WHERE f.kind='segment' AND f.segment_id=s.id AND f.relative_path=s.relative_path) "
        "AND s.id NOT IN (SELECT value FROM json_each(?)) ORDER BY s.id LIMIT ?", (after, skipped, limit)).fetchall()
    # Everything below the first one still waiting has its link, or is set aside (unreadable, storage away) until the next whole look.
    _looked[database] = (int(rows[0]["id"]) - 1 if rows else newest, now if whole else scanned)
    return rows


async def append_pending() -> int:
    """Hash newly indexed segments and record removals; returns how many were handled, so callers repeat while it is non-zero.

    Hashing reads files outside any transaction."""
    from .storage import recording_file_path

    appended = 0

    def waiting() -> list[sqlite3.Row]:
        with connect() as db:
            return _unchained(db, BATCH)

    pending = await asyncio.to_thread(waiting)  # Off the event loop: the background worker's other loops and its heartbeat share it
    if not pending and not await asyncio.to_thread(_removals_waiting):
        return 0
    hashed: list[tuple[sqlite3.Row, str]] = []
    for segment in pending:
        try:
            path = recording_file_path(segment["relative_path"])
            hashed.append((segment, await asyncio.to_thread(file_sha256, path)))
        except (OSError, ValueError):
            _unreadable[segment["id"]] = time.monotonic()  # Missing at hashing time: tried again later, reported as pending until then.
            appended += 1
    def store() -> int:
        added = 0
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for segment, digest in hashed:
                    still = db.execute("SELECT 1 FROM recording_segments WHERE id=? AND relative_path=?", (segment["id"], segment["relative_path"])).fetchone()
                    if still is None or db.execute("SELECT 1 FROM footage_chain WHERE kind='segment' AND segment_id=? AND relative_path=?",
                                                   (segment["id"], segment["relative_path"])).fetchone():
                        continue
                    _append(db, {"camera_id": segment["camera_id"], "kind": "segment", "segment_id": segment["id"], "relative_path": segment["relative_path"],
                                 "starts_at": segment["starts_at"], "ends_at": segment["ends_at"], "size_bytes": int(segment["size_bytes"]), "sha256": digest})
                    added += 1
                for removal in db.execute("SELECT * FROM footage_removals ORDER BY removed_at, segment_id").fetchall():
                    link = db.execute("SELECT * FROM footage_chain f WHERE f.kind='segment' AND f.segment_id=? AND f.camera_id=? AND NOT EXISTS "
                                      "(SELECT 1 FROM footage_chain r WHERE r.kind='removed' AND r.segment_id=f.segment_id AND r.relative_path=f.relative_path AND r.seq>f.seq) "
                                      "ORDER BY f.seq DESC LIMIT 1", (removal["segment_id"], removal["camera_id"])).fetchone()
                    if link is not None:
                        _append(db, {"camera_id": link["camera_id"], "kind": "removed", "segment_id": link["segment_id"], "relative_path": link["relative_path"],
                                     "starts_at": link["starts_at"], "ends_at": link["ends_at"], "size_bytes": link["size_bytes"], "sha256": link["sha256"]})
                        added += 1
                    db.execute("DELETE FROM footage_removals WHERE segment_id=?", (removal["segment_id"],))
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        return added

    return appended + await asyncio.to_thread(store)


def sign_heads() -> int:
    """Sign every camera head that changed since its last signature, only where the new links extend the signed ones intact."""
    signed = 0
    with connect() as db:
        # Read first which heads moved: with nothing new, nothing is written and the write lock is not taken (#280, #352).
        if not any((last := _latest_signature(db, row["camera_id"])) is None or int(last["upto_seq"]) < int(row["seq"]) for row in _heads(db)):
            return 0
        db.execute("BEGIN IMMEDIATE")
        try:
            private, public = _key(db)
            for row in _heads(db):
                last = _latest_signature(db, row["camera_id"])
                if last is not None and int(last["upto_seq"]) >= int(row["seq"]):
                    continue
                since = int(last["upto_seq"]) if last is not None else 1
                links = [dict(link) for link in db.execute("SELECT * FROM footage_chain WHERE camera_id=? AND seq>=? ORDER BY seq", (row["camera_id"], since))]
                if fault := _from_signature(db, row["camera_id"], last, links):
                    # Recorded once, with its incident, and signing resumes from the head: checking from the last good signature again
                    # found the same fault every minute for good, and nothing recorded on the camera afterwards was ever signed (#507).
                    _record_break(db, private, public, row["camera_id"], last, fault, links[-1])
                head = links[-1]["link_hash"]
                signature = base64.b64encode(private.sign(signed_message(row["camera_id"], int(row["seq"]), head))).decode()
                db.execute("INSERT INTO footage_chain_signatures(camera_id,upto_seq,head_hash,signature,public_key,signed_at) VALUES(?,?,?,?,?,?)",
                           (row["camera_id"], row["seq"], head, signature, public, _now()))
                signed += 1
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
    return signed


# --- keeping the chain from growing for ever (#500) ---------------------------------------------------------------------------------------
#
# Every recorded minute added a link when it was recorded and another when retention removed it, and a signature each minute, and nothing
# was ever deleted: 0.77 GB a camera a year, in the one database file. What is kept now (the owner's decision on #500):
#
# * every link of footage still on disk, and of footage that was removed, for KEEP_DAYS after the removal;
# * older than that, the links are replaced by a checkpoint: the newest signed link that everything before is old enough to drop. The link
#   stays, with its signature, and the chain that remains verifies from it exactly as it verified from any signature before;
# * signatures: each one for the last THIN_AFTER_DAYS, one an hour for anything older.
#
# An export carries its own extract of the chain inside its package, so an export made before its links were dropped still verifies offline.

THIN_AFTER_DAYS = 30
PRUNE_EVERY_SECONDS = 3600.0
PRUNE_BATCH = 1000  # Rows deleted in one step
PRUNE_SCAN = 20_000  # Links looked at in one step, in search of the next checkpoint
PRUNE_AGAIN_SECONDS = 5.0  # After a step that dropped something: more may be left


def keep_days() -> int:
    """How long the links of removed footage are kept (NVR_FOOTAGE_CHAIN_KEEP_DAYS: 365 by default, 90 days to 10 years)."""
    import os

    try:
        days = int(os.environ.get("NVR_FOOTAGE_CHAIN_KEEP_DAYS", "365"))
    except ValueError:
        days = 365
    return max(90, min(days, 3650))


def checkpoint(db: sqlite3.Connection, camera_id: str) -> sqlite3.Row | None:
    """Where a camera's kept chain begins, or None when nothing has been dropped."""
    return db.execute("SELECT * FROM footage_chain_checkpoints WHERE camera_id=?", (camera_id,)).fetchone()


def _droppable_upto(db: sqlite3.Connection, camera_id: str, cutoff: str, after: int) -> int:
    """The last link after ``after`` that is old enough to drop, counting on from there: ``after`` itself when none is. It stops at the
    first link that must be kept, and after PRUNE_SCAN links, so a step's reading is bounded however long the chain has grown."""
    last = after
    for _ in range(PRUNE_SCAN // PRUNE_BATCH):
        links = db.execute("SELECT seq,kind,segment_id,relative_path,created_at FROM footage_chain WHERE camera_id=? AND seq>? ORDER BY seq LIMIT ?",
                           (camera_id, last, PRUNE_BATCH)).fetchall()
        for link in links:
            if link["created_at"] >= cutoff:
                return last  # Written within the kept period
            if link["kind"] == "segment":
                if db.execute("SELECT 1 FROM recording_segments WHERE id=? AND relative_path=?", (link["segment_id"], link["relative_path"])).fetchone():
                    return last  # Its footage is still on disk (held, or kept for an alarm): its link stays, and so does all that follows
                removal = db.execute("SELECT created_at FROM footage_chain WHERE segment_id=? AND kind='removed' AND camera_id=? AND relative_path=? ORDER BY seq DESC LIMIT 1",
                                     (link["segment_id"], camera_id, link["relative_path"])).fetchone()
                if removal is not None and removal["created_at"] >= cutoff:
                    return last  # Removed within the kept period
            last = int(link["seq"])
        if len(links) < PRUNE_BATCH:
            break
    return last


def _thin_signatures(db: sqlite3.Connection, camera_id: str, now: datetime, keep_seq: int) -> int:
    """Leave one signature an hour among those older than THIN_AFTER_DAYS (the last of each hour). The newest signature and the checkpoint's
    are always kept. A link then sits in a stretch of up to an hour between two signed points, where it sat in a minute."""
    old = (now - timedelta(days=THIN_AFTER_DAYS)).isoformat()
    newest = db.execute("SELECT MAX(upto_seq) FROM footage_chain_signatures WHERE camera_id=?", (camera_id,)).fetchone()[0]
    doomed = [row[0] for row in db.execute(
        "SELECT upto_seq FROM footage_chain_signatures s WHERE camera_id=? AND signed_at<? AND upto_seq NOT IN (?,?) AND upto_seq<("
        "SELECT MAX(upto_seq) FROM footage_chain_signatures h WHERE h.camera_id=s.camera_id AND h.signed_at<? AND substr(h.signed_at,1,13)=substr(s.signed_at,1,13)) LIMIT ?",
        (camera_id, old, newest or 0, keep_seq, old, PRUNE_BATCH))]
    if doomed:
        db.execute(f"DELETE FROM footage_chain_signatures WHERE camera_id=? AND upto_seq IN ({','.join('?' * len(doomed))})", (camera_id, *doomed))
    return len(doomed)


def _drop_links(db: sqlite3.Connection, camera_id: str, before_seq: int) -> int:
    dropped = db.execute("DELETE FROM footage_chain WHERE camera_id=? AND seq IN (SELECT seq FROM footage_chain WHERE camera_id=? AND seq<? ORDER BY seq LIMIT ?)",
                         (camera_id, camera_id, before_seq, PRUNE_BATCH)).rowcount
    db.execute("UPDATE footage_chain_checkpoints SET links_removed=links_removed+? WHERE camera_id=?", (dropped, camera_id))
    return dropped


def prune_camera(camera_id: str, now: datetime | None = None) -> dict[str, int]:
    """Drop what is past the kept period from one camera's chain, a bounded amount: links before a new checkpoint, the signatures before
    it, and all but one signature an hour among the old ones. Reads first; the write lock is taken only when there is something to drop."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=keep_days())).isoformat()
    done = {"links": 0, "signatures": 0}
    with connect() as db:
        before = checkpoint(db, camera_id)
        kept_seq = int(before["seq"]) if before else 0
        if kept_seq and db.execute("SELECT 1 FROM footage_chain WHERE camera_id=? AND seq<? LIMIT 1", (camera_id, kept_seq)).fetchone():
            done["links"] = _drop_links(db, camera_id, kept_seq)  # Left before the checkpoint by an earlier, bounded step
        else:
            # Everything up to the checkpoint was old enough when it was made, so the look goes on from there.
            droppable = _droppable_upto(db, camera_id, cutoff, kept_seq)
            # The new checkpoint is a signed link: the newest signature at or before the last droppable link, valid as it stands now.
            mark = db.execute("SELECT * FROM footage_chain_signatures WHERE camera_id=? AND upto_seq<=? AND upto_seq>? ORDER BY upto_seq DESC LIMIT 1",
                              (camera_id, droppable, kept_seq)).fetchone() if droppable > kept_seq else None
            if mark is not None and not _signature_error(db, camera_id, mark, _key(db)[1]):
                at = db.execute("SELECT seq,link_hash,ends_at FROM footage_chain WHERE camera_id=? AND seq=?", (camera_id, mark["upto_seq"])).fetchone()
                db.execute("BEGIN IMMEDIATE")
                try:
                    # The checkpoint in the same change as the first links dropped: the chain is never without its start.
                    db.execute("INSERT INTO footage_chain_checkpoints(camera_id,seq,link_hash,kept_from) VALUES(?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET "
                               "seq=excluded.seq,link_hash=excluded.link_hash,kept_from=excluded.kept_from,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                               (camera_id, at["seq"], at["link_hash"], at["ends_at"]))  # Footage after the checkpoint's has its links after it
                    done["links"] = _drop_links(db, camera_id, int(at["seq"]))
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")
                kept_seq = int(at["seq"])
        if kept_seq and db.execute("SELECT 1 FROM footage_chain_signatures WHERE camera_id=? AND upto_seq<? LIMIT 1", (camera_id, kept_seq)).fetchone():
            done["signatures"] += db.execute("DELETE FROM footage_chain_signatures WHERE camera_id=? AND upto_seq IN (SELECT upto_seq FROM footage_chain_signatures WHERE camera_id=? AND upto_seq<? LIMIT ?)",
                                             (camera_id, camera_id, kept_seq, PRUNE_BATCH)).rowcount
        done["signatures"] += _thin_signatures(db, camera_id, now, kept_seq)
    return done


def prune(now: datetime | None = None) -> dict[str, int]:
    """One bounded step for every camera with a chain. Returns what was dropped; a caller that sees anything dropped calls again."""
    with connect() as db:
        cameras = [row["camera_id"] for row in _heads(db)]
    total = {"links": 0, "signatures": 0}
    for camera_id in cameras:
        done = prune_camera(camera_id, now)
        total = {key: total[key] + done[key] for key in total}
    return total


def verify_signature(public_key_b64: str, camera_id: str, upto_seq: int, head_hash: str, signature_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(base64.b64decode(signature_b64), signed_message(camera_id, upto_seq, head_hash))
        return True
    except (InvalidSignature, ValueError):
        return False


def find_break(links: list[dict[str, Any]], first_prev: str | None = None, first_seq: int | None = None) -> tuple[int, str] | None:
    """None when the links hash correctly in sequence; otherwise the first link that does not, and what is wrong with it."""
    previous = first_prev
    expected_seq = first_seq if first_seq is not None else (links[0]["seq"] if links else 0)
    for link in links:
        if link["seq"] != expected_seq:
            return expected_seq, f"link {expected_seq} is missing from the chain"
        if previous is not None and link["prev_hash"] != previous:
            return int(link["seq"]), f"link {link['seq']} does not follow the link before it"
        if link_hash(link) != link["link_hash"]:
            return int(link["seq"]), f"link {link['seq']} was changed after it was written"
        previous = link["link_hash"]
        expected_seq += 1
    return None


def check_links(links: list[dict[str, Any]], first_prev: str | None = None) -> str | None:
    """None when the links hash correctly in sequence; otherwise what is wrong."""
    found = find_break(links, first_prev)
    return found[1] if found else None


# --- a chain that no longer matches its signature (#507) ----------------------------------------------------------------------------------
#
# The owner's decision on the ticket: signing resumes by itself, at once, from a signed break record that names the camera, the link that no
# longer matched and when it was noticed. The record is permanent (the table's triggers refuse a delete or a change), any verification or
# export that spans it says so, and its incident stays active until an administrator acknowledges it from a browser session with a note.

BREAK_FAULT = "footage_chain"


def break_message(record: dict[str, Any]) -> bytes:
    """What a break record's signature covers: every field of the record but the acknowledgement."""
    return json.dumps({"kind": "nvr-footage-chain-break:v1", **{key: record[key] for key in ("camera_id", "broken_seq", "signed_seq", "resumed_seq", "head_hash", "reason",
                                                                                           "detected_at")}}, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _resumes_after_break(db: sqlite3.Connection, camera_id: str, seq: int) -> bool:
    """Whether signing resumed at link ``seq`` after a break: that link is where the chain is checked from again, as it stands, whatever
    happened to it before."""
    return db.execute("SELECT 1 FROM footage_chain_breaks WHERE camera_id=? AND resumed_seq=?", (camera_id, seq)).fetchone() is not None


def _from_signature(db: sqlite3.Connection, camera_id: str, signature: sqlite3.Row | None, links: list[dict[str, Any]]) -> tuple[int, str] | None:
    """Whether ``links``, from a signed link to the head, still extend that signature intact: None, or the first link that does not."""
    if signature is None:
        return find_break(links, GENESIS, 1)
    since = int(signature["upto_seq"])
    if not links or links[0]["seq"] != since or links[0]["link_hash"] != signature["head_hash"]:
        return since, f"link {since} no longer matches its signature"
    if _resumes_after_break(db, camera_id, since):
        return find_break(links[1:], links[0]["link_hash"], since + 1)
    return find_break(links, None)


def chain_fault(db: sqlite3.Connection, camera_id: str) -> tuple[int, str] | None:
    """Read only: whether a camera's links since its last signature extend it intact (the nightly self-test asks this of every camera)."""
    last = _latest_signature(db, camera_id)
    since = int(last["upto_seq"]) if last is not None else 1
    links = [dict(link) for link in db.execute("SELECT * FROM footage_chain WHERE camera_id=? AND seq>=? ORDER BY seq", (camera_id, since))]
    return _from_signature(db, camera_id, last, links) if links else None


def _record_break(db: sqlite3.Connection, private: Ed25519PrivateKey, public: str, camera_id: str, last: sqlite3.Row | None,
                  fault: tuple[int, str], head: dict[str, Any]) -> str:
    """The signed, permanent record of a break, and its incident. Signing then resumes from ``head``."""
    from .incidents import sync_incident

    break_id, detected_at = str(uuid.uuid4()), _now()
    broken_seq, reason = fault
    record = {"camera_id": camera_id, "broken_seq": broken_seq, "signed_seq": int(last["upto_seq"]) if last is not None else 0, "resumed_seq": int(head["seq"]),
              "head_hash": head["link_hash"], "reason": reason, "detected_at": detected_at}
    signature = base64.b64encode(private.sign(break_message(record))).decode()
    db.execute("INSERT INTO footage_chain_breaks(id,camera_id,broken_seq,signed_seq,resumed_seq,head_hash,reason,detected_at,signature,public_key) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)",
               (break_id, camera_id, broken_seq, record["signed_seq"], record["resumed_seq"], record["head_hash"], reason, detected_at, signature, public))
    camera = db.execute("SELECT name FROM cameras WHERE id=?", (camera_id,)).fetchone()
    name = camera["name"] if camera is not None else camera_id
    sync_incident(db, incident_key=f"{BREAK_FAULT}:{break_id}", camera_id=camera_id if camera is not None else None, fault_type=BREAK_FAULT,
                  summary=f"The signed footage of {name} no longer matches its signature at link {broken_seq} ({reason}); signing carries on from link {head['seq']}",
                  active=True, details={"break_id": break_id, "camera_id": camera_id, "camera": name, "link": broken_seq, "reason": reason,
                                        "resumed_at_link": int(head["seq"]), "detected_at": detected_at})
    logger.error("The signed footage chain of camera %s no longer matches its signature: %s. The break is recorded and signing carries on from link %s",
                 camera_id, reason, head["seq"])
    return break_id


def break_view(row: sqlite3.Row, installation_key: str | None = None) -> dict[str, Any]:
    view = {key: row[key] for key in ("id", "camera_id", "broken_seq", "signed_seq", "resumed_seq", "reason", "detected_at", "acknowledged_by",
                                      "acknowledged_at", "acknowledgement_note")}
    view["signature_valid"] = verify_break(dict(row))
    if installation_key is not None:
        view["signature_valid"] = view["signature_valid"] and row["public_key"] == installation_key
    return view


def verify_break(record: dict[str, Any]) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(record["public_key"])).verify(base64.b64decode(record["signature"]), break_message(record))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def _breaks_spanning(db: sqlite3.Connection, camera_id: str, first_seq: int, last_seq: int) -> list[sqlite3.Row]:
    """Breaks whose links, from the one that no longer matched up to where signing resumed, overlap links first..last."""
    return db.execute("SELECT * FROM footage_chain_breaks WHERE camera_id=? AND broken_seq<=? AND resumed_seq>? ORDER BY resumed_seq",
                      (camera_id, last_seq, first_seq)).fetchall()


def acknowledge_break(db: sqlite3.Connection, break_id: str, actor_id: int, note: str) -> sqlite3.Row | None:
    """An administrator has seen the break: noted on the record, once, in the security log, and its incident recovers."""
    from .audit import record as audit_record
    from .incidents import sync_incident

    row = db.execute("SELECT * FROM footage_chain_breaks WHERE id=?", (break_id,)).fetchone()
    if row is None or row["acknowledged_at"] is not None:
        return row
    db.execute("UPDATE footage_chain_breaks SET acknowledged_by=?,acknowledged_at=?,acknowledgement_note=? WHERE id=?", (actor_id, _now(), note, break_id))
    audit_record(db, actor_id, "footage_chain_break_acknowledged", "footage_chain_break", break_id, camera_id=row["camera_id"], link=row["broken_seq"], note=note)
    sync_incident(db, incident_key=f"{BREAK_FAULT}:{break_id}", camera_id=None, fault_type=BREAK_FAULT, summary="", active=False,
                  details={"break_id": break_id, "acknowledged_by": actor_id, "note": note})
    return db.execute("SELECT * FROM footage_chain_breaks WHERE id=?", (break_id,)).fetchone()


LONGEST_LINK = timedelta(hours=24)  # No recorded segment is longer: a link that touches a range starts within this of it


def _signature_error(db: sqlite3.Connection, camera_id: str, signature: sqlite3.Row, installation_key: str) -> str | None:
    """None when a stored signature is this installation's, is valid, and is over the link it names as that link stands now."""
    link = db.execute("SELECT link_hash FROM footage_chain WHERE camera_id=? AND seq=?", (camera_id, signature["upto_seq"])).fetchone()
    if signature["public_key"] != installation_key or link is None or link["link_hash"] != signature["head_hash"] \
            or not verify_signature(signature["public_key"], camera_id, int(signature["upto_seq"]), signature["head_hash"], signature["signature"]):
        return "the chain's signature does not match it"
    return None


def _check_stretches(db: sqlite3.Connection, camera_id: str, seqs: list[int], installation_key: str) -> str | None:
    """Check the parts of the chain that hold these links: each from the signature before a link to the first signature that covers it.

    A camera's head is signed about once a minute, so a link sits in a short stretch between two signed points. Hashing that stretch
    from one valid signature to the next proves the link is what was signed, without reading the chain from the camera's first link."""
    checked_upto = 0
    kept = checkpoint(db, camera_id)
    for seq in seqs:
        if seq <= checked_upto:
            continue
        before, covering = _signature_before(db, camera_id, seq), _signature_at_or_after(db, camera_id, seq)
        if covering is not None and int(covering["upto_seq"]) == seq and _resumes_after_break(db, camera_id, seq):
            # Where signing resumed after a break (#507): this link is proven by its own signature, whatever the broken stretch before it.
            if error := _signature_error(db, camera_id, covering, installation_key):
                return error
            checked_upto = seq
            continue
        if before is None and kept is not None and seq >= int(kept["seq"]):
            # The kept chain begins at the checkpoint's signed link (#500): that signature is the point to check from.
            before = db.execute("SELECT * FROM footage_chain_signatures WHERE camera_id=? AND upto_seq=?", (camera_id, kept["seq"])).fetchone()
            if before is None:
                return "the signature the kept chain begins at is missing"
        for signature in (before, covering):
            if signature is not None and (error := _signature_error(db, camera_id, signature, installation_key)):
                return error
        first = int(before["upto_seq"]) if before is not None else 1
        last = int(covering["upto_seq"]) if covering is not None else None  # Not signed yet: checked to the head, and reported as unsigned
        stretch = [dict(row) for row in db.execute("SELECT * FROM footage_chain WHERE camera_id=? AND seq>=? AND seq<=? ORDER BY seq",
                                                   (camera_id, first, last if last is not None else 1 << 62))]
        if before is not None and (not stretch or stretch[0]["seq"] != first or stretch[0]["link_hash"] != before["head_hash"]):
            return f"link {first} no longer matches its signature"
        if before is not None and _resumes_after_break(db, camera_id, first):
            found = find_break(stretch[1:], stretch[0]["link_hash"], first + 1)  # Signing resumed here after a break (#507): checked from it as signed
            if found:
                return found[1]
        elif error := check_links(stretch, GENESIS if before is None else None):
            return error
        checked_upto = stretch[-1]["seq"] if stretch else seq
    return None


class VerificationCancelled(Exception):
    """The person who asked for a verification left or cancelled it: it stops between two files (#508)."""


def verify(camera_id: str, start: datetime, end: datetime, *, include_archived: bool = True, cancel: Any = None,
           progress: Any = None) -> dict[str, Any]:
    """Check a camera's footage in [start, end) against its signed chain.

    ``include_archived`` False leaves footage that has moved to a cold tier unread (``not_checked_archived``): reading it downloads it from an
    object store, which only an administrator may start (#508). ``cancel`` is an event checked between files; ``progress`` is called with
    (files checked, files in all) as the check goes."""
    from .storage import _hot_file_path, recording_file_path, segment_available

    # Stored times are UTC ISO text, so compare in UTC.
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    overdue = (datetime.now(timezone.utc) - OVERDUE).isoformat()
    with connect() as db:
        after = db.execute("SELECT after_segment_id FROM footage_chain_start").fetchone()["after_segment_id"]
        segments = [dict(row) for row in db.execute(
            "SELECT * FROM recording_segments WHERE camera_id=? AND starts_at<? AND ends_at>? ORDER BY starts_at, id",
            (camera_id, end.isoformat(), start.isoformat()))]
        _, installation_key = _key(db)
        # Only the links this range is about: those of its segments, and those whose own time falls in it (footage since removed). The
        # whole chain used to be loaded and re-hashed for any range: 69 MB to check one hour of a month-old camera (#352).
        links: dict[int, dict[str, Any]] = {}
        ids = [segment["id"] for segment in segments]
        for offset in range(0, len(ids), 500):
            part = ids[offset:offset + 500]
            for row in db.execute(f"SELECT * FROM footage_chain WHERE camera_id=? AND segment_id IN ({','.join('?' * len(part))})", (camera_id, *part)):
                links[int(row["seq"])] = dict(row)
        for row in db.execute("SELECT * FROM footage_chain WHERE camera_id=? AND starts_at<? AND starts_at>=? AND ends_at>?",
                              (camera_id, end.isoformat(), (start - LONGEST_LINK).isoformat(), start.isoformat())):
            links[int(row["seq"])] = dict(row)
        kept = checkpoint(db, camera_id)
        if kept is not None:
            # Links before the checkpoint are on their way out (they are dropped a bounded number at a time): not part of the kept chain.
            links = {seq: link for seq, link in links.items() if seq >= int(kept["seq"])}
        chain_error = _check_stretches(db, camera_id, sorted(links), installation_key)
        # A break the range spans: its links were not what was signed when the break was noticed, so the footage they cover is not proven (#507).
        breaks = [break_view(row, installation_key) for row in _breaks_spanning(db, camera_id, min(links), max(links))] if links else []
        if breaks:
            first_break = breaks[0]
            chain_error = (f"the signed chain was broken at link {first_break['broken_seq']} ({first_break['reason']}), noticed {first_break['detected_at']}; "
                           f"footage before signing resumed at link {first_break['resumed_seq']} is not proven")
        signature = _latest_signature(db, camera_id)
        signed_upto = 0
        if signature is not None:
            error = _signature_error(db, camera_id, signature, installation_key)
            if error:
                chain_error = chain_error or error
            else:
                signed_upto = int(signature["upto_seq"])
    links = list(links.values())
    removed = {(link["segment_id"], link["relative_path"]) for link in links if link["kind"] == "removed"}
    chained = {(link["segment_id"], link["relative_path"]): link for link in sorted(links, key=lambda link: link["seq"]) if link["kind"] == "segment"}
    in_range = [link for link in chained.values() if link["starts_at"] < end.isoformat() and link["ends_at"] > start.isoformat()]
    present = {(segment["id"], segment["relative_path"]): segment for segment in segments}
    results = []
    for done, segment in enumerate(segments):
        if cancel is not None and cancel.is_set():
            raise VerificationCancelled
        if progress is not None:
            progress(done, len(segments))
        key = (segment["id"], segment["relative_path"])
        link = chained.get(key)
        entry = {"segment_id": segment["id"], "starts_at": segment["starts_at"], "ends_at": segment["ends_at"]}
        if link is None and segment["id"] <= after:
            entry["status"] = "not_signed"
        elif link is None:
            # Not hashed yet. Footage long closed whose file is gone was lost before it could be hashed.
            try:
                lost = segment["ends_at"] < overdue and not segment_available(segment["relative_path"])
            except (OSError, ValueError):
                lost = True
            entry["status"] = "missing" if lost else "pending"
        elif (link["starts_at"], link["ends_at"]) != (segment["starts_at"], segment["ends_at"]):
            entry["status"] = "out_of_order"
        elif segment["unavailable_at"]:
            entry["status"] = "unavailable"
        elif not include_archived and segment["tier_id"] and not _hot_file(_hot_file_path, segment["relative_path"]):
            entry["status"] = "not_checked_archived"  # On a cold tier: reading it would download it; not a problem, and not proven either
        else:
            try:
                path = recording_file_path(segment["relative_path"])
                entry["status"] = "missing" if not path.is_file() else ("ok" if file_sha256(path) == link["sha256"] else "altered")
            except (OSError, ValueError):
                entry["status"] = "missing"
            if entry["status"] == "ok" and link["seq"] > signed_upto:
                entry["status"] = "signature_missing" if link["created_at"] < overdue else "ok_unsigned_yet"
        results.append(entry)
    # Links whose segment is gone without a removal record: deleted outside the NVR.
    for link in in_range:
        key = (link["segment_id"], link["relative_path"])
        if key not in present and key not in removed:
            results.append({"segment_id": link["segment_id"], "starts_at": link["starts_at"], "ends_at": link["ends_at"], "status": "missing"})
    if progress is not None:
        progress(len(segments), len(segments))
    results.sort(key=lambda item: (item["starts_at"], item["segment_id"]))
    problems = [entry for entry in results if entry["status"] in PROBLEMS]
    # Footage older than the kept chain: its links were dropped KEEP_DAYS after it was removed. That is said as such, and is never a problem.
    before_kept_chain = kept is not None and start.isoformat() < kept["kept_from"]
    return {"camera_id": camera_id, "start": start.isoformat(), "end": end.isoformat(),
            "valid": chain_error is None and not problems, "chain_error": chain_error, "signed_upto": signed_upto, "breaks": breaks,
            "public_key": installation_key, "segments": results, "problems": problems,
            "before_kept_chain": before_kept_chain, "kept_from": kept["kept_from"] if kept is not None else None}


def _hot_file(hot_path: Any, relative_path: str) -> bool:
    try:
        return hot_path(relative_path).is_file()
    except OSError:
        return False


def extract(db: sqlite3.Connection, camera_id: str, segment_ids: Iterable[int]) -> dict[str, Any] | None:
    """The chain from the first exported segment's link to the first signature that covers the last one, for offline verification.

    It used to run on to the latest signature, so a one-minute export of month-old footage carried a month of links for each camera:
    25.7 MB a camera (#352). Segments not signed yet when the export is made are simply past the end, and reported as unsigned."""
    wanted = set(segment_ids)
    first, last = db.execute(f"SELECT MIN(seq),MAX(seq) FROM footage_chain WHERE camera_id=? AND kind='segment' AND segment_id IN ({','.join('?' * len(wanted)) or 'NULL'})",
                             (camera_id, *wanted)).fetchone() if wanted else (None, None)
    if first is None:
        return None
    # The first signature covering the last segment; if that one is not signed yet, the newest signature covers those that are.
    signature = _signature_at_or_after(db, camera_id, int(last)) or _latest_signature(db, camera_id)
    if signature is None or first > signature["upto_seq"]:
        return None
    links = [dict(row) for row in db.execute("SELECT camera_id,seq,kind,segment_id,relative_path,starts_at,ends_at,size_bytes,sha256,prev_hash,link_hash "
                                             "FROM footage_chain WHERE camera_id=? AND seq BETWEEN ? AND ? ORDER BY seq", (camera_id, first, signature["upto_seq"]))]
    breaks = [{key: row[key] for key in ("camera_id", "broken_seq", "signed_seq", "resumed_seq", "head_hash", "reason", "detected_at", "signature", "public_key")}
              for row in _breaks_spanning(db, camera_id, int(first), int(signature["upto_seq"]))]
    resumed = _resumes_after_break(db, camera_id, int(first))  # The extract starts where signing resumed after a break: checked from it as signed
    return {"camera_id": camera_id, "links": links, "breaks": breaks, "starts_after_break": resumed,
            "signature": {key: signature[key] for key in ("upto_seq", "head_hash", "signature", "public_key", "signed_at")}}


def verify_extract(chain: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Offline: the extract hashes correctly up to its signed head, the signature holds for its public key,
    and every exported source segment in it has the SHA-256 of its link. Sources the extract does not
    reach (recorded before signing began, or not yet hashed) are listed as unsigned, not as mismatches."""
    links = chain.get("links") or []
    signature = chain.get("signature") or {}
    breaks = chain.get("breaks") or []
    if chain.get("starts_after_break") and links:
        found = find_break(links[1:], links[0]["link_hash"], int(links[0]["seq"]) + 1)
        error = found[1] if found else None
    else:
        error = check_links(links)
    for record in breaks:  # The export spans a break (#507): said, with the break's own signature checked
        if not verify_break(record) or record.get("public_key") != signature.get("public_key"):
            error = error or "a break record in the chain extract is not validly signed"
        error = error or (f"the signed chain was broken at link {record['broken_seq']} ({record['reason']}), noticed {record['detected_at']}; "
                          f"footage before signing resumed at link {record['resumed_seq']} is not proven")
    if not error and (not links or links[-1]["seq"] != signature.get("upto_seq") or links[-1]["link_hash"] != signature.get("head_hash")):
        error = "the chain extract does not end at its signed head"
    if not error and not verify_signature(signature.get("public_key", ""), chain.get("camera_id", ""), int(signature.get("upto_seq", 0)),
                                          signature.get("head_hash", ""), signature.get("signature", "")):
        error = "the chain extract's signature is not valid"
    by_segment = {(link["segment_id"], link["relative_path"]): link for link in links if link["kind"] == "segment"}
    mismatched, unsigned = [], []
    for source in sources:
        link = by_segment.get((source["segment_id"], source["relative_path"]))
        if link is None:
            unsigned.append(source["segment_id"])
        elif link["sha256"] != source["sha256"]:
            mismatched.append(source["segment_id"])
    if not error and mismatched:
        error = f"exported source segments {mismatched} do not match the signed chain"
    result = {"valid": error is None, "error": error, "public_key": signature.get("public_key"), "unsigned": unsigned}
    if breaks:
        result["breaks"] = [{key: record.get(key) for key in ("broken_seq", "resumed_seq", "reason", "detected_at")} for record in breaks]
    return result


class FootageChainManager:
    """Appends new segments and removals to the chains and signs the heads, in the background worker."""

    def __init__(self, poll_seconds: float = 5.0, sign_seconds: float = SIGN_INTERVAL_SECONDS) -> None:
        self.poll_seconds = poll_seconds
        self.sign_seconds = sign_seconds
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="footage-chain")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        last_signed = 0.0
        last_pruned = loop.time()  # Not at start-up: the first step is an hour in
        while True:
            try:
                while await append_pending():
                    pass
                if loop.time() - last_signed >= self.sign_seconds:
                    await asyncio.to_thread(sign_heads)
                    last_signed = loop.time()
                if loop.time() - last_pruned >= PRUNE_EVERY_SECONDS:
                    dropped = await asyncio.to_thread(prune)
                    if dropped["links"] or dropped["signatures"]:
                        logger.info("Footage chain: %d links and %d signatures past the kept period were dropped", dropped["links"], dropped["signatures"])
                        last_pruned = loop.time() - PRUNE_EVERY_SECONDS + PRUNE_AGAIN_SECONDS  # More may be left: a step is bounded
                    else:
                        last_pruned = loop.time()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - tried again next poll
                logger.exception("Footage chain update failed; retrying")
            await asyncio.sleep(self.poll_seconds)
