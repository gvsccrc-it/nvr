"""Storage tiering (#144): older footage moves to a cold tier, a local path or an S3-compatible bucket, and stays playable.

**Policy.** A storage volume (the default one, or one a camera group is assigned to) has at most one policy: a cold tier, and the age after which a segment moves.

**A move** copies the segment to the tier, reads the copy back and checks its SHA-256 and size against the file it came from (and against the footage chain's link, so
altered footage is never carried over), switches the segment's row to the tier (it keeps its id and ``relative_path``, which is what the chain, holds, exports and cases
refer to) and only then deletes the hot file. Each step can be repeated: the copy overwrites, the switch is conditional, and a hot file left behind (it was being played)
is removed by a later pass. A crash anywhere leaves the segment readable from the hot or the cold copy, once. Held footage and footage in a queued or running export
is never moved, and a move gives way if either happens while it copies. Copies are rate limited.

**Reading.** ``storage.recording_file_path`` finds a segment wherever it is: a local cold tier is read in place; an object store's segment is fetched into a read-through cache
(verified against the digest it was moved with, bounded in size, least recently used out) and played from there, so everything that reads footage (playback, exports,
verification, cases) works across tiers unchanged.

**Deleting** footage (retention, an administrator's delete, a camera's footage) removes the cold copy and any cached copy as well.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .credentials import decrypt, encrypt
from .incidents import sync_incident
from .s3 import S3Client, S3Error
from .store import connect
from app.clock import now as _now, parse as _parse

logger = logging.getLogger("nvr.tiering")

DEFAULT_RATE_KBPS = 20480
CACHE_DEFAULT_BYTES = 2 * 1024**3
FAILURES_BEFORE_INCIDENT = 3
BATCH = 20
POLL_SECONDS = 30.0
_fetch_locks: dict[str, list] = {}  # A cache file's lock and how many fetches hold or wait for it; dropped when none do, so it never grows
_fetch_guard = threading.Lock()


EVICT_GRACE_SECONDS = 120


class TierError(OSError):
    """A cold tier that cannot be used (unreachable, refused, a copy that did not match)."""


# --- tiers ------------------------------------------------------------------------------------------------------------------------------------

@dataclass
class Tier:
    id: str
    name: str
    kind: str
    local_path: str | None
    s3_endpoint: str | None
    s3_bucket: str | None
    s3_region: str
    s3_prefix: str
    access_key: str | None
    secret_key: str | None
    read_back: bool = False  # Read every file back from an object store after upload, instead of trusting the store's checksum (#486)
    checksums: str | None = None  # Whether the object store keeps the checksum it is sent: 'yes', 'no', or None until a move finds out


def load_tier(db: sqlite3.Connection, tier_id: str) -> Tier | None:
    row = db.execute("SELECT * FROM cold_tiers WHERE id=?", (tier_id,)).fetchone()
    if row is None:
        return None
    credentials = json.loads(decrypt(row["credentials_ciphertext"])) if row["credentials_ciphertext"] is not None else {}
    return Tier(row["id"], row["name"], row["kind"], row["local_path"], row["s3_endpoint"], row["s3_bucket"], row["s3_region"], row["s3_prefix"],
                credentials.get("access_key"), credentials.get("secret_key"), bool(row["read_back"]), row["checksums"])


def seal_credentials(access_key: str, secret_key: str) -> bytes:
    return encrypt(json.dumps({"access_key": access_key, "secret_key": secret_key}))


class Rate:
    """The rate limit of a policy: ``kib_per_second`` kibibytes (1,024 bytes) a second, which the page shows as MiB a second.

    One is shared by every move of a policy (``rate_for``), so the limit holds overall, not per segment: passes now follow each other at
    once while footage is waiting (#486), and a fresh allowance per segment would have let a run of small segments go far faster. It is
    a token bucket holding at most ``BURST_SECONDS`` of allowance, so time spent idle is not saved up as a burst; a call never sleeps
    more than five seconds (so a stop is noticed), and what it still owes is carried to the next."""

    BURST_SECONDS = 1.0

    def __init__(self, kib_per_second: int) -> None:
        self.kib_per_second = kib_per_second
        self.per_second = max(1, kib_per_second) * 1024
        self.allowance = self.per_second * self.BURST_SECONDS
        self.at = time.monotonic()
        self.lock = threading.Lock()

    def wait(self, size: int) -> None:
        with self.lock:
            now = time.monotonic()
            self.allowance = min(self.per_second * self.BURST_SECONDS, self.allowance + (now - self.at) * self.per_second) - size
            self.at = now
            owed = -self.allowance / self.per_second
        if owed > 0:
            time.sleep(min(owed, 5.0))

    __call__ = wait


def throttle(rate_kbps: int) -> Callable[[int], None]:
    """A function to call with the size of each chunk moved: it sleeps so the average stays at or under the rate (KiB a second)."""
    return Rate(rate_kbps).wait


_rates: dict[str, Rate] = {}
_rates_guard = threading.Lock()


def rate_for(volume_id: str, rate_kbps: int) -> Rate:
    """The rate every move of this volume's policy shares; a changed limit starts a new one."""
    with _rates_guard:
        rate = _rates.get(volume_id)
        if rate is None or rate.kib_per_second != rate_kbps:
            rate = _rates[volume_id] = Rate(rate_kbps)
        return rate


class LocalStore:
    kind = "local"

    def __init__(self, tier: Tier) -> None:
        self.root = Path(tier.local_path or "")

    def path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if self.root.resolve() not in candidate.parents:
            raise TierError("The cold tier's path would leave its folder")
        return candidate

    def present(self) -> bool:
        """Whether the tier's folder is there with anything in it. A disk that is not mounted leaves a missing or an empty folder, and a tier
        that holds footage is never empty: so "nothing there at all" means the tier is away, not that its footage was deleted."""
        try:
            with os.scandir(self.root) as entries:
                return next(entries, None) is not None
        except OSError:
            return False

    def put(self, source: Path, key: str, sha256: str, size: int, rate: Callable[[int], None] | None = None) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.partial")  # Two movers of one segment never share a partial file
        for stale in target.parent.glob(f".{target.name}.*.partial"):  # Left by a copy that died; one being written is not an hour old
            if time.time() - stale.stat().st_mtime > 3600:
                stale.unlink(missing_ok=True)
        try:
            with source.open("rb") as reader, partial.open("wb") as writer:
                while chunk := reader.read(1024 * 1024):
                    if rate:
                        rate(len(chunk))
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)

    def read_digest(self, key: str, rate: Callable[[int], None] | None = None) -> tuple[str, int]:
        digest, size = hashlib.sha256(), 0
        with self.path(key).open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if rate:
                    rate(len(chunk))
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def confirm(self, key: str, sha256: str, size: int, rate: Callable[[int], None] | None = None) -> str:
        """Read the copy back in full (it costs only disk time) and check it: how it was checked, or TierError."""
        if self.read_digest(key, rate) != (sha256, size):
            raise TierError(MISMATCH)
        return "read_back"

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    def check(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / f".nvr-check-{os.getpid()}"
        try:
            probe.write_bytes(b"nvr cold tier check")
            if probe.read_bytes() != b"nvr cold tier check":
                raise TierError("The cold tier did not keep what was written to it")
        finally:
            probe.unlink(missing_ok=True)


class ObjectStore:
    kind = "s3"

    def __init__(self, tier: Tier, client: S3Client | None = None) -> None:
        self.tier = tier
        self.client = client or S3Client(tier.s3_endpoint or "", tier.s3_bucket or "", tier.access_key or "", tier.secret_key or "", region=tier.s3_region, prefix=tier.s3_prefix)

    def put(self, source: Path, key: str, sha256: str, size: int, rate: Callable[[int], None] | None = None) -> None:
        self.client.put_file(key, source, sha256, size, rate=rate)

    def read_digest(self, key: str, rate: Callable[[int], None] | None = None) -> tuple[str, int]:
        return self.client.read_digest(key, rate=rate)

    def confirm(self, key: str, sha256: str, size: int, rate: Callable[[int], None] | None = None) -> str:
        """Check the uploaded copy: how it was checked ('checksum' or 'read_back'), or TierError.

        The upload was signed with the file's SHA-256 and sent it as the object's checksum, so the store itself refuses a body that does not
        match. Here, a request for the object's details confirms its size and the checksum the store kept, with no download (the owner's
        decision on #486). A store that keeps no checksum, or a tier set to read every file back, is read back in full, as before."""
        if not self.tier.read_back:
            details = self.client.details(key)
            if details is None:
                raise TierError("The copy is not on the cold tier; the next pass sends it again")
            kept_size, kept_sha = details
            if kept_sha is not None:
                _note_checksums(self.tier, "yes")
                if (kept_sha, kept_size) != (sha256, size):
                    raise TierError(MISMATCH)
                return "checksum"
            _note_checksums(self.tier, "no")
        if self.read_digest(key, rate) != (sha256, size):
            raise TierError(MISMATCH)
        return "read_back"

    def fetch(self, key: str, target: Path) -> tuple[str, int]:
        return self.client.download(key, target)

    def delete(self, key: str) -> None:
        self.client.delete(key)

    def check(self) -> None:
        self.client.check()

    def close(self) -> None:
        self.client.close()


MISMATCH = "The copy on the cold tier did not match the footage; the next pass sends it again"  # The key is shared with any other mover: it is overwritten, not deleted


def _note_checksums(tier: Tier, kept: str) -> None:
    """Remember whether the store keeps checksums, for the tiering page; written only when it changes."""
    if tier.checksums == kept:
        return
    tier.checksums = kept
    with connect() as db:
        db.execute("UPDATE cold_tiers SET checksums=? WHERE id=?", (kept, tier.id))
    if kept == "no":
        logger.warning("The cold tier %s keeps no checksums: every file is read back in full after it is uploaded", tier.name)


def open_store(tier: Tier) -> LocalStore | ObjectStore:
    return LocalStore(tier) if tier.kind == "local" else ObjectStore(tier)


def close_store(store: Any) -> None:
    if hasattr(store, "close"):
        store.close()


def note_health(tier_id: str, error: str | None) -> None:
    """Remember how the tier is doing; a tier that keeps failing raises an incident, which recovers with its first success."""
    with connect() as db:
        tier = db.execute("SELECT name,failures FROM cold_tiers WHERE id=?", (tier_id,)).fetchone()
        if tier is None:
            return
        failures = 0 if error is None else int(tier["failures"]) + 1
        db.execute("UPDATE cold_tiers SET failures=?,last_error=?,last_checked_at=? WHERE id=?", (failures, error, _now().isoformat(), tier_id))
        sync_incident(db, incident_key=f"tiering:{tier_id}", camera_id=None, fault_type="tiering", summary=f"The cold storage tier {tier['name']} is failing: {error}" if error else "",
                      active=failures >= FAILURES_BEFORE_INCIDENT, details={"tier": tier["name"], "error": error})


# --- reading footage that has moved --------------------------------------------------------------------------------------------------------------

def cache_dir() -> Path:
    from .store import database_path

    return Path(os.environ.get("NVR_TIER_CACHE_PATH", str(database_path().parent / "tier-cache")))


def cache_limit() -> int:
    try:
        return max(64 * 1024 * 1024, int(os.environ.get("NVR_TIER_CACHE_BYTES", str(CACHE_DEFAULT_BYTES))))
    except ValueError:
        return CACHE_DEFAULT_BYTES


def _cache_file(row: sqlite3.Row) -> Path:
    return cache_dir() / f"{row['id']}-{(row['tier_sha256'] or '')[:16]}.mp4"


def tiered_row(relative_path: str) -> sqlite3.Row | None:
    with connect() as db:
        return db.execute("SELECT id,camera_id,relative_path,tier_id,tier_key,tier_sha256,size_bytes FROM recording_segments WHERE relative_path=? AND tier_id IS NOT NULL", (relative_path,)).fetchone()


def cold_path(relative_path: str) -> Path | None:
    """Where a tiered segment can be read from: its file on a local tier, or its verified copy in the cache. None when it has not moved."""
    row = tiered_row(relative_path)
    if row is None:
        return None
    with connect() as db:
        tier = load_tier(db, row["tier_id"])
    if tier is None:
        raise TierError("The cold tier this footage moved to no longer exists")
    store = open_store(tier)
    try:
        if isinstance(store, LocalStore):
            return store.path(row["tier_key"])
        return _fetch_cached(store, tier, row)
    finally:
        close_store(store)


def _fetch_cached(store: ObjectStore, tier: Tier, row: sqlite3.Row) -> Path:
    target = _cache_file(row)
    key = str(target)
    with _fetch_guard:
        entry = _fetch_locks.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            fetched = _fetch_into(store, tier, row, target)
    finally:
        with _fetch_guard:
            entry[1] -= 1
            if entry[1] == 0:
                _fetch_locks.pop(key, None)
    if fetched:
        evict(keep=target)
    return target


def _fetch_into(store: ObjectStore, tier: Tier, row: sqlite3.Row, target: Path) -> bool:
    """Fetch a segment from the store into its cache file unless a whole copy is already there; True when it was fetched. The caller holds
    the file's lock."""
    if target.is_file() and target.stat().st_size == int(row["size_bytes"]):
        os.utime(target)
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{os.getpid()}.partial")
    try:
        digest, size = store.fetch(row["tier_key"], partial)
        if digest != row["tier_sha256"] or size != int(row["size_bytes"]):
            note_health(tier.id, "A segment came back from the cold tier different from the one that was sent")
            raise TierError("The footage came back from the cold tier changed")
        os.replace(partial, target)
    except S3Error as exc:
        note_health(tier.id, str(exc))
        raise TierError(str(exc)) from None
    finally:
        partial.unlink(missing_ok=True)
    return True


def evict(keep: Path | None = None, limit: int | None = None) -> int:
    """Keep the cache within its size: the least recently used copies go, except any being played."""
    from .retention import unlink_if_unleased

    directory = cache_dir()
    if not directory.is_dir():
        return 0
    for stale in directory.glob(".*.partial"):  # Left by a fetch that died; one being written is minutes old at most
        try:
            if time.time() - stale.stat().st_mtime > 3600:
                stale.unlink(missing_ok=True)
        except OSError:
            pass
    # A copy fetched or read moments ago is about to be played (the caller has not taken its lease yet): it is the last to go.
    files = sorted((path for path in directory.glob("*.mp4") if path != keep and time.time() - path.stat().st_mtime > EVICT_GRACE_SECONDS), key=lambda path: path.stat().st_mtime)
    total = sum(path.stat().st_size for path in directory.glob("*.mp4"))
    removed = 0
    for path in files:
        if total <= (limit if limit is not None else cache_limit()):
            break
        size = path.stat().st_size
        if unlink_if_unleased(path):
            total -= size
            removed += 1
    return removed


def segment_present(relative_path: str) -> bool:
    """Whether a tiered segment's footage can be had, without fetching it: the file for a local tier, the tier itself for an object store."""
    row = tiered_row(relative_path)
    if row is None:
        return False
    with connect() as db:
        tier = load_tier(db, row["tier_id"])
    if tier is None:
        return False
    if tier.kind == "local":
        try:
            return LocalStore(tier).path(row["tier_key"]).is_file()
        except TierError:
            return False
    return True


# --- deleting footage that has moved ------------------------------------------------------------------------------------------------------------

def local_files(row: sqlite3.Row | dict[str, Any], hot: Path | None) -> list[Path]:
    """Every file on this machine that holds a segment: its hot file, its file on a local tier, its cached copy."""
    found: list[Path] = [hot] if hot is not None and hot.exists() else []
    if row["tier_id"]:
        with connect() as db:
            tier = load_tier(db, row["tier_id"])
        if tier is not None and tier.kind == "local":
            store = LocalStore(tier)
            if not store.present():
                from .storage import StorageUnavailable
                raise StorageUnavailable("The cold tier's folder is not there")  # Not mounted: its footage is not gone (#338)
            try:
                path = store.path(row["tier_key"])
                if path.exists():
                    found.append(path)
            except TierError:
                pass
        elif tier is not None:
            cached = _cache_file({"id": row["id"], "tier_sha256": row["tier_sha256"]})  # type: ignore[arg-type]
            if cached.exists():
                found.append(cached)
    return found


def discard_objects(rows: list[Any]) -> None:
    """Remove the copies in object stores of segments that have been deleted. One that cannot be removed now is remembered and tried again, so nothing is left behind for good."""
    for row in rows:
        if not row["tier_id"]:
            continue
        with connect() as db:
            tier = load_tier(db, row["tier_id"])
        if tier is None or tier.kind != "s3":
            continue
        store = ObjectStore(tier)
        try:
            store.delete(row["tier_key"])
        except S3Error as exc:
            note_health(tier.id, str(exc))
            with connect() as db:
                db.execute("INSERT OR IGNORE INTO tier_orphans(tier_id,tier_key) VALUES(?,?)", (tier.id, row["tier_key"]))
        finally:
            store.close()


def sweep_orphans(opener: Callable[[Tier], Any] = open_store) -> int:
    """Try again to remove objects whose segments are gone."""
    removed = 0
    with connect() as db:
        orphans = db.execute("SELECT * FROM tier_orphans ORDER BY created_at LIMIT 50").fetchall()
    for orphan in orphans:
        with connect() as db:
            tier = load_tier(db, orphan["tier_id"])
        if tier is None:
            with connect() as db:
                db.execute("DELETE FROM tier_orphans WHERE tier_id=? AND tier_key=?", (orphan["tier_id"], orphan["tier_key"]))
            continue
        store = opener(tier)
        try:
            store.delete(orphan["tier_key"])
        except (OSError, S3Error):
            continue
        finally:
            close_store(store)
        with connect() as db:
            db.execute("DELETE FROM tier_orphans WHERE tier_id=? AND tier_key=?", (orphan["tier_id"], orphan["tier_key"]))
        removed += 1
    return removed


# --- moving -----------------------------------------------------------------------------------------------------------------------------------

def policy_for(db: sqlite3.Connection, camera_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT p.* FROM tier_policies p WHERE p.volume_id=COALESCE((SELECT g.storage_volume_id FROM cameras c JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?),'default')",
        (camera_id,)).fetchone()


def blocked(db: sqlite3.Connection, segment_id: int, now: datetime) -> str | None:
    """Why a segment must not move now: held, or in a queued or running export."""
    if db.execute("SELECT 1 FROM recording_holds WHERE segment_id=? AND released_at IS NULL AND (expires_at IS NULL OR expires_at>?) LIMIT 1", (segment_id, now.isoformat())).fetchone():
        return "held"
    if db.execute("SELECT 1 FROM export_sources s JOIN exports e ON e.id=s.export_id WHERE s.segment_id=? AND e.status IN ('queued','running') LIMIT 1", (segment_id,)).fetchone():
        return "exporting"
    return None


def _hot_candidate(relative_path: str) -> Path:
    from .recording import recordings_path

    return (recordings_path() / relative_path).resolve()


def _forget_move(segment_id: int) -> None:
    with connect() as db:
        db.execute("DELETE FROM tier_moves WHERE segment_id=?", (segment_id,))


def move_segment(segment_id: int, now: datetime | None = None, opener: Callable[[Tier], Any] = open_store) -> str:
    """Move one segment to its cold tier. Returns what happened: moved, already, held, exporting, no_policy, gone, unavailable, altered, failed, young."""
    from .footage_chain import file_sha256
    from .retention import unlink_if_unleased

    now = now or _now()
    with connect() as db:
        row = db.execute("SELECT * FROM recording_segments WHERE id=?", (segment_id,)).fetchone()
        if row is None:
            begun = db.execute("SELECT tier_id,tier_key FROM tier_moves WHERE segment_id=?", (segment_id,)).fetchone()
            if begun is not None:  # The footage was deleted part way through a move: its copy is nobody's now
                db.execute("INSERT OR IGNORE INTO tier_orphans(tier_id,tier_key) VALUES(?,?)", (begun["tier_id"], begun["tier_key"]))
                db.execute("DELETE FROM tier_moves WHERE segment_id=?", (segment_id,))
            return "gone"
        if row["tier_id"]:
            return "already"
        policy = policy_for(db, row["camera_id"])
        if policy is None or not policy["enabled"]:
            return "no_policy"
        if row["unavailable_at"]:
            return "unavailable"
        if _parse(row["ends_at"]) > now - timedelta(days=int(policy["after_days"])):
            return "young"
        tier = load_tier(db, policy["tier_id"])
        reason = blocked(db, segment_id, now)
        move = db.execute("SELECT * FROM tier_moves WHERE segment_id=?", (segment_id,)).fetchone()
    key = row["relative_path"]
    if tier is None:
        return "no_policy"
    store = opener(tier)
    try:
        if reason:
            if move is not None:  # A copy begun before the segment was held or exported is taken back
                _discard_copy(store, key, segment_id)
            return reason
        source = _hot_candidate(row["relative_path"])
        if not source.is_file():
            # Remembered, with when: it is not picked again for MISSING_RETRY, so it cannot use up every pass, and it is counted for the
            # tier's status (#485). If the file comes back (a volume that was away) the next try moves it and clears this.
            with connect() as db:
                db.execute("INSERT INTO tier_moves(segment_id,tier_id,tier_key,state,last_error,updated_at) VALUES(?,?,?,'copying',?,?) "
                           "ON CONFLICT(segment_id) DO UPDATE SET last_error=excluded.last_error,updated_at=excluded.updated_at",
                           (segment_id, tier.id, key, f"{MISSING_ERROR}: it is looked for again in an hour", now.isoformat()))
            return "missing"
        sha, size = file_sha256(source), source.stat().st_size
        with connect() as db:
            link = db.execute("SELECT sha256 FROM footage_chain WHERE kind='segment' AND segment_id=? AND relative_path=? ORDER BY seq DESC LIMIT 1", (segment_id, row["relative_path"])).fetchone()
            if link is not None and link["sha256"] != sha:
                db.execute("INSERT INTO tier_moves(segment_id,tier_id,tier_key,state,last_error) VALUES(?,?,?,'copying',?) ON CONFLICT(segment_id) DO UPDATE SET last_error=excluded.last_error,updated_at=?",
                           (segment_id, tier.id, key, "The footage no longer matches its signed chain: it is not moved", now.isoformat()))
                return "altered"
            db.execute("INSERT INTO tier_moves(segment_id,tier_id,tier_key,state,sha256,size_bytes,attempts) VALUES(?,?,?,'copying',?,?,1) "
                       "ON CONFLICT(segment_id) DO UPDATE SET tier_id=excluded.tier_id,tier_key=excluded.tier_key,state='copying',sha256=excluded.sha256,size_bytes=excluded.size_bytes,"
                       "attempts=attempts+1,last_error=NULL,updated_at=?", (segment_id, tier.id, key, sha, size, now.isoformat()))
        rate = rate_for(policy["volume_id"], int(policy["rate_kbps"]))
        try:
            store.put(source, key, sha, size, rate)
            _confirm(store, key, sha, size, rate)
        except (OSError, S3Error) as exc:
            message = str(exc)
            note_health(tier.id, message)
            with connect() as db:
                db.execute("UPDATE tier_moves SET last_error=?,updated_at=? WHERE segment_id=?", (message[:300], now.isoformat(), segment_id))
            return "failed"
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                again = blocked(db, segment_id, now)
                current = db.execute("SELECT tier_id,size_bytes FROM recording_segments WHERE id=?", (segment_id,)).fetchone()
                if current is not None and current["tier_id"] and not again:
                    db.execute("ROLLBACK")  # Another mover switched it first, to this same object: leave the object alone
                    return "already"
                if again or current is None or int(current["size_bytes"]) != size:
                    db.execute("ROLLBACK")
                    _discard_copy(store, key, segment_id)
                    return again or "gone"
                db.execute("UPDATE recording_segments SET tier_id=?,tier_key=?,tier_sha256=?,tiered_at=? WHERE id=? AND tier_id IS NULL", (tier.id, key, sha, now.isoformat(), segment_id))
                db.execute("UPDATE tier_moves SET state='switched',last_error=NULL,updated_at=? WHERE segment_id=?", (now.isoformat(), segment_id))
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        note_health(tier.id, None)
    finally:
        close_store(store)
    # The segment now lives on the tier; the hot file goes unless someone is playing it, and then a later pass takes it.
    if unlink_if_unleased(source):
        _forget_move(segment_id)
    return "moved"


def _confirm(store: Any, key: str, sha256: str, size: int, rate: Callable[[int], None]) -> None:
    if hasattr(store, "confirm"):
        store.confirm(key, sha256, size, rate)
    elif store.read_digest(key, rate) != (sha256, size):
        raise TierError(MISMATCH)


def _discard_copy(store: Any, key: str, segment_id: int) -> None:
    try:
        store.delete(key)
    except (OSError, S3Error):
        return  # Left for the next pass to find: the move row stays
    _forget_move(segment_id)


def sweep_switched() -> int:
    """Remove the hot files of segments that have moved but whose hot file was in use (or whose removal was interrupted)."""
    from .retention import unlink_if_unleased

    done = 0
    with connect() as db:
        rows = db.execute("SELECT m.segment_id,s.relative_path FROM tier_moves m LEFT JOIN recording_segments s ON s.id=m.segment_id WHERE m.state='switched'").fetchall()
    for row in rows:
        if row["relative_path"] is None:
            _forget_move(row["segment_id"])
            continue
        hot = _hot_candidate(row["relative_path"])
        if not hot.exists() or unlink_if_unleased(hot):
            _forget_move(row["segment_id"])
            done += 1
    return done


MISSING_ERROR = "The footage file is missing from its volume"
MISSING_RETRY = timedelta(hours=1)  # A file that is not there is looked for again this often, not on every pass


# The oldest due footage still on the recorder's disks. ``recording_segments_tier_due_idx`` (ends_at, for rows with no tier and not
# unavailable) is read in order and stops at ``LIMIT``: this scanned and sorted the whole table on every pass (#486).
CANDIDATES_SQL = (
    "SELECT s.id FROM recording_segments s INDEXED BY recording_segments_tier_due_idx JOIN cameras c ON c.id=s.camera_id LEFT JOIN camera_groups g ON g.id=c.group_id "
    "WHERE s.tier_id IS NULL AND s.unavailable_at IS NULL AND s.ends_at<? AND COALESCE(g.storage_volume_id,'default')=? "
    "AND NOT EXISTS (SELECT 1 FROM tier_moves m WHERE m.segment_id=s.id AND (m.last_error LIKE 'The footage no longer matches%' OR (m.last_error IS NOT NULL AND m.updated_at>?) "
    "OR (m.last_error LIKE ? AND m.updated_at>?))) "
    "AND NOT EXISTS (SELECT 1 FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>?)) "
    "AND NOT EXISTS (SELECT 1 FROM export_sources x JOIN exports e ON e.id=x.export_id WHERE x.segment_id=s.id AND e.status IN ('queued','running')) "
    "ORDER BY s.ends_at LIMIT ?")


def candidates(db: sqlite3.Connection, policy: sqlite3.Row, now: datetime, limit: int) -> list[int]:
    """The policy's oldest segments that can move now. Footage that is held or in an export, and footage whose file was missing when it
    was last tried, is left out here, as retention and headroom leave held footage out: a pass took the 20 oldest whatever they were,
    so 20 held segments at the front were picked again on every pass and nothing behind them ever moved (#485)."""
    cutoff = (now - timedelta(days=int(policy["after_days"]))).isoformat()
    return [row[0] for row in db.execute(CANDIDATES_SQL, (cutoff, policy["volume_id"], (now - timedelta(minutes=5)).isoformat(), MISSING_ERROR + "%",
                                                          (now - MISSING_RETRY).isoformat(), now.isoformat(), limit))]


def set_aside(db: sqlite3.Connection, policy: sqlite3.Row, now: datetime, limit: int) -> dict[str, int]:
    """How many of the policy's due segments a pass leaves because they are held or in an export, counted up to ``limit`` of each: what a
    pass reports, now that it no longer picks them."""
    cutoff = (now - timedelta(days=int(policy["after_days"]))).isoformat()
    due = ("FROM recording_segments s INDEXED BY recording_segments_tier_due_idx JOIN cameras c ON c.id=s.camera_id LEFT JOIN camera_groups g ON g.id=c.group_id "
           "WHERE s.tier_id IS NULL AND s.unavailable_at IS NULL AND s.ends_at<? AND COALESCE(g.storage_volume_id,'default')=? AND ")
    held = "EXISTS (SELECT 1 FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>?))"
    exporting = "EXISTS (SELECT 1 FROM export_sources x JOIN exports e ON e.id=x.export_id WHERE x.segment_id=s.id AND e.status IN ('queued','running'))"
    counts = {
        "held": db.execute(f"SELECT COUNT(*) FROM (SELECT 1 {due}{held} LIMIT ?)", (cutoff, policy["volume_id"], now.isoformat(), limit)).fetchone()[0],
        "exporting": db.execute(f"SELECT COUNT(*) FROM (SELECT 1 {due}NOT {held} AND {exporting} LIMIT ?)", (cutoff, policy["volume_id"], now.isoformat(), limit)).fetchone()[0],
    }
    return {name: count for name, count in counts.items() if count}


def missing_files(db: sqlite3.Connection, tier_id: str | None = None) -> int:
    """How many segments could not be moved because their file was not on the volume when last tried."""
    if tier_id is None:
        return db.execute("SELECT COUNT(*) FROM tier_moves WHERE last_error LIKE ?", (MISSING_ERROR + "%",)).fetchone()[0]
    return db.execute("SELECT COUNT(*) FROM tier_moves WHERE tier_id=? AND last_error LIKE ?", (tier_id, MISSING_ERROR + "%")).fetchone()[0]


def run_once(now: datetime | None = None, limit: int = BATCH, opener: Callable[[Tier], Any] = open_store) -> dict[str, int]:
    """One pass: finish interrupted moves, then move up to ``limit`` of each policy's oldest eligible segments."""
    return drain(now, limit, opener)[0]


def drain(now: datetime | None = None, limit: int = BATCH, opener: Callable[[Tier], Any] = open_store, until: float | None = None,
          stop: threading.Event | None = None) -> tuple[dict[str, int], bool]:
    """Finish interrupted moves, then move each policy's oldest eligible segments, ``limit`` at a time: one batch each, or with ``until``
    (a ``time.monotonic()`` deadline), batch after batch until nothing is left or the time is up. Returns what happened and whether
    footage is still waiting (the time ran out with some left), so the next pass starts at once instead of after ``POLL_SECONDS``.

    A pass took 20 segments a policy and slept 30 s: at most 40 a minute, while 200 cameras record 200 (#486). The rate limit is shared by
    every move of a policy (``rate_for``), so going on at once does not go faster than it allows. ``stop``, once set, ends the pass after
    the segment being moved, so stopping the worker does not wait for the rest of a long pass."""
    fixed = now
    outcomes: dict[str, int] = {}

    def count(result: str) -> None:
        outcomes[result] = outcomes.get(result, 0) + 1

    sweep_switched()
    sweep_orphans(opener)
    with connect() as db:
        interrupted = [row[0] for row in db.execute("SELECT segment_id FROM tier_moves WHERE state='copying' AND last_error IS NULL ORDER BY segment_id")]
        policies = db.execute("SELECT * FROM tier_policies WHERE enabled=1").fetchall()
    for segment_id in interrupted:
        count(move_segment(segment_id, fixed or _now(), opener))
    waiting = False
    for policy in policies:
        if not _volume_present(policy["volume_id"]):
            count("waiting_for_storage")  # Its disk is not mounted: there is nothing there to move (#479)
            continue
        first = True
        while True:
            when = fixed or _now()
            with connect() as db:
                chosen = candidates(db, policy, when, limit)
                if first:
                    for reason, number in set_aside(db, policy, when, limit).items():
                        outcomes[reason] = outcomes.get(reason, 0) + number
            first = False
            results = []
            for segment_id in chosen:
                if stop is not None and stop.is_set():
                    return outcomes, True
                results.append(move_segment(segment_id, when, opener))
                count(results[-1])
            if until is None or len(chosen) < limit or "moved" not in results:
                break  # One batch asked for; or nothing more is due; or nothing in this batch could move (a failing tier), which the next pass retries
            if time.monotonic() >= until:
                waiting = True
                break
    return outcomes, waiting


def _volume_present(volume_id: str) -> bool:
    from . import volume_marker
    from .storage import volume_path

    try:
        return volume_marker.verified(volume_id, volume_path(volume_id))
    except (OSError, ValueError):
        return False


PASS_SECONDS = 25.0  # How long one pass goes on while footage is waiting, before it measures the backlog and starts the next


class TieringManager:
    """Moves footage to the cold tiers, one segment at a time and rate limited, in the background: pass after pass while footage is waiting,
    then every ``poll_seconds``. Every few minutes it measures each volume's backlog (``measure_backlog``)."""

    def __init__(self, poll_seconds: float = POLL_SECONDS, pass_seconds: float = PASS_SECONDS) -> None:
        self.poll_seconds, self.pass_seconds = poll_seconds, pass_seconds
        self.task: Any = None
        self.stopping = threading.Event()

    async def start(self) -> None:
        import asyncio

        self.stopping.clear()
        self.task = asyncio.create_task(self._run(), name="tiering")

    async def stop(self) -> None:
        import asyncio

        self.stopping.set()  # A pass in its thread ends after the segment it is moving
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    def pass_once(self) -> bool:
        """One pass and, when due, a backlog measurement; whether footage is still waiting."""
        waiting = False
        try:
            _, waiting = drain(until=time.monotonic() + self.pass_seconds, stop=self.stopping)
        finally:
            measure_backlog()
        return waiting

    async def _run(self) -> None:
        import asyncio

        while True:
            waiting = False
            try:
                waiting = await asyncio.to_thread(self.pass_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed pass is tried again at the next
                logger.exception("A tiering pass failed")
            await asyncio.sleep(0 if waiting else self.poll_seconds)


# --- the backlog ------------------------------------------------------------------------------------------------------------------------------

BACKLOG_EVERY = timedelta(minutes=10)  # How often each volume's backlog is measured
BACKLOG_SAMPLES = 7  # Kept per volume: an hour of measurements, and the one before it
BACKLOG_GRACE = timedelta(days=1)  # Footage this far past its move age is overdue, not just in the next batch


def backlog(db: sqlite3.Connection, policy: sqlite3.Row, now: datetime) -> tuple[int, str | None]:
    """How many of the policy's segments are due to move and not held (held footage never moves, so it is not a backlog), and when the
    oldest of them ended."""
    cutoff = (now - timedelta(days=int(policy["after_days"]))).isoformat()
    row = db.execute(
        "SELECT COUNT(*),MIN(s.ends_at) FROM recording_segments s INDEXED BY recording_segments_tier_due_idx JOIN cameras c ON c.id=s.camera_id "
        "LEFT JOIN camera_groups g ON g.id=c.group_id WHERE s.tier_id IS NULL AND s.unavailable_at IS NULL AND s.ends_at<? AND COALESCE(g.storage_volume_id,'default')=? "
        "AND NOT EXISTS (SELECT 1 FROM recording_holds h WHERE h.segment_id=s.id AND h.released_at IS NULL AND (h.expires_at IS NULL OR h.expires_at>?))",
        (cutoff, policy["volume_id"], now.isoformat())).fetchone()
    return int(row[0]), row[1]


def growing(samples: list[list[Any]]) -> bool:
    """Whether the backlog kept growing: an hour of measurements, none smaller than the one before, the last larger than the first. One
    that keeps up rises and falls between passes; one that cannot only rises."""
    if len(samples) < BACKLOG_SAMPLES:
        return False
    counts = [count for _, count in samples]
    return counts[-1] > counts[0] and all(later >= earlier for earlier, later in zip(counts, counts[1:]))


def measure_backlog(now: datetime | None = None, force: bool = False) -> dict[str, dict[str, Any]]:
    """Measure each enabled policy's backlog (at most every ``BACKLOG_EVERY``, unless ``force``), keep the last hour of measurements and
    raise an incident for a volume whose backlog keeps growing while its oldest waiting footage is overdue; it recovers once the backlog
    stops growing. A volume whose disk is away, or a policy turned off, is not measured and its incident recovers."""
    now = now or _now()
    measured: dict[str, dict[str, Any]] = {}
    with connect() as db:
        policies = db.execute("SELECT * FROM tier_policies").fetchall()
        kept = {row["volume_id"]: row for row in db.execute("SELECT * FROM tier_backlog")}
    for policy in policies:
        volume = policy["volume_id"]
        last = kept.get(volume)
        if not force and last is not None and _parse(last["measured_at"]) > now - BACKLOG_EVERY + timedelta(seconds=30):
            continue
        active = bool(policy["enabled"]) and _volume_present(volume)
        with connect() as db:
            if not active:
                db.execute("DELETE FROM tier_backlog WHERE volume_id=?", (volume,))
                sync_incident(db, incident_key=f"tiering-backlog:{volume}", camera_id=None, fault_type="tiering", summary="", active=False, details={"volume": volume})
                continue
            count, oldest = backlog(db, policy, now)
            samples = (json.loads(last["samples_json"]) if last is not None else []) + [[now.isoformat(), count]]
            samples = samples[-BACKLOG_SAMPLES:]
            overdue = oldest is not None and _parse(oldest) < now - timedelta(days=int(policy["after_days"])) - BACKLOG_GRACE
            behind = overdue and growing(samples)
            db.execute("INSERT INTO tier_backlog(volume_id,segments,oldest_ends_at,samples_json,measured_at) VALUES(?,?,?,?,?) ON CONFLICT(volume_id) DO UPDATE SET "
                       "segments=excluded.segments,oldest_ends_at=excluded.oldest_ends_at,samples_json=excluded.samples_json,measured_at=excluded.measured_at",
                       (volume, count, oldest, json.dumps(samples), now.isoformat()))
            sync_incident(db, incident_key=f"tiering-backlog:{volume}", camera_id=None, fault_type="tiering",
                          summary=(f"Tiering on volume {volume} is falling behind: {count} segments are waiting to move and the backlog has grown for an hour. "
                                   "Older footage may be deleted to keep disk headroom instead of being moved: raise the policy's rate limit or check the tier") if behind else "",
                          active=behind, details={"volume": volume, "segments": count, "oldest_ends_at": oldest})
        measured[volume] = {"segments": count, "oldest_ends_at": oldest, "growing": behind}
    return measured


def backlog_view(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The last measurement of each volume's backlog, for the tiering page."""
    return {row["volume_id"]: {"segments": row["segments"], "oldest_ends_at": row["oldest_ends_at"], "measured_at": row["measured_at"],
                               "growing": growing(json.loads(row["samples_json"]))} for row in db.execute("SELECT * FROM tier_backlog")}
