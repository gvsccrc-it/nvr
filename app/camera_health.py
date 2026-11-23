"""Camera health (#137): a fleet view of what each camera reports and what the NVR measures, read from stored values only.

Nothing here starts a stream. The camera's own report (make, model, firmware, the bitrate and frame rate it is set to) is read over ONVIF every
few minutes and kept with the time of the last contact; the measured bitrate and frame rate come from the recordings; reconnects and dropped
frames are the recorder's counters (``metric_counters``: reconnects are counted by a database trigger on the recorder's state, dropped frames
from FFmpeg's progress report). A value the camera or recorder has not reported is ``None``, shown as "Not reported", never a guess.

Firmware is compared within a make and model across the cameras the reader may see: where those cameras run different versions, those
below the newest are flagged as outdated.
"""
from __future__ import annotations

from app.child_env import child_environment

import asyncio
import csv
import io
import json
import logging
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from . import metrics
from .credentials import decrypt
from .store import connect
from app.clock import now as _now, parse as _parse

logger = logging.getLogger("nvr.camera_health")

RECONNECTS, FRAMES_DROPPED = "reconnects", "frames_dropped"
SAMPLE_SECONDS = 600
MEASURE_WINDOW = timedelta(minutes=30)
ONVIF_CONCURRENCY = 4
# How many cameras' recordings are measured at once (#477). Each measurement can run ffprobe for up to 15 s on a busy volume, in a thread
# of the worker's shared pool: unbounded, 200 cameras due together took every thread, and webhook results, PTZ tours and capability
# writes waited behind them. A stalled measurement holds one of these places, not the others.
MEASURE_CONCURRENCY = 4
# Each camera is checked again after SAMPLE_SECONDS moved by an amount of its own, within this spread, so that cameras first checked in one
# pass (at start-up, or when added together) do not all come due together for ever (#477).
SAMPLE_SPREAD_SECONDS = 300
NOT_REPORTED = "Not reported"
UP_STATES = ("recording", "buffering")  # The same two as in the trigger of migration 33
PRERELEASE = frozenset({"alpha", "beta", "rc", "pre", "dev", "preview", "a", "b"})


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


# --- the recorder's dropped-frame counter ----------------------------------------------------------------------------------------------------

class ProgressCounter:
    """Turns FFmpeg's ``-progress`` report into increments of the dropped-frame counter: the report's ``drop_frames`` is a running total for the process."""

    def __init__(self, camera_id: str) -> None:
        self.camera_id, self.seen = camera_id, 0

    def feed(self, line: str) -> int:
        """One line of the report, counted at once; the frames dropped since the last one (0 for most lines). Blocking: see ``watch_progress``."""
        return self.count(self.parse(line))

    def parse(self, line: str) -> int:
        """The frames one line of the report says were dropped since the last one (0 for most lines). Touches nothing."""
        key, _, value = line.strip().partition("=")
        if key != "drop_frames":
            return 0
        try:
            total = int(value)
        except ValueError:
            return 0
        added, self.seen = max(0, total - self.seen), max(self.seen, total)
        return added

    def count(self, added: int) -> int:
        """Add ``added`` dropped frames to the camera's counter; what was counted (0 when the database refused it). A database write."""
        if not added:
            return 0
        try:
            with connect() as db:
                metrics.bump(db, FRAMES_DROPPED, self.camera_id, added)
        except sqlite3.Error:
            logger.warning("Dropped frames could not be counted", exc_info=True)
            return 0
        return added


async def watch_progress(stream: asyncio.StreamReader | None, camera_id: str) -> None:
    """Read an FFmpeg process's progress report to its end, counting dropped frames. Never raises: counting must not disturb recording."""
    if stream is None:
        return
    counter = ProgressCounter(camera_id)
    try:
        while line := await stream.readline():
            added = counter.parse(line.decode("utf-8", errors="replace"))
            if added:
                # Counted in a thread: the write waits for the database's lock, and the recorder's loop (every camera's watch and its sign of
                # life to the supervisor) must not wait with it (#470).
                await asyncio.to_thread(counter.count, added)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("The progress report of %s could not be read", camera_id, exc_info=True)


# --- sampling ---------------------------------------------------------------------------------------------------------------------------------

async def read_onvif(row: sqlite3.Row) -> dict[str, Any]:
    """What the camera says about itself over ONVIF: make, model, firmware and the settings of the profile being recorded. Raises OnvifError."""
    from .onvif_client import OnvifClient
    from .onvif_profiles import choose_profiles

    credentials = json.loads(decrypt(row["motion_credentials_ciphertext"])) if row["motion_credentials_ciphertext"] else {}
    host = urlsplit(row["device_service_url"]).hostname or row["camera_ip"] or ""  # The port is in the device service address that is located next
    async with OnvifClient(host, credentials.get("username", ""), credentials.get("password", "")) as client:
        device = await client.locate_device(row["device_service_url"])
        info = await client.device_information(device)
        found: dict[str, Any] = {"manufacturer": info.manufacturer or None, "model": info.model or None, "firmware": info.firmware or None}
        try:
            profiles = await client.profiles(await client.services(device))
        except Exception:  # noqa: BLE001 - the device information is worth keeping when the media service will not answer
            profiles = []
    chosen = next((item for item in profiles if item.token == row["onvif_profile_token"]), None) or (choose_profiles(profiles).record if profiles else None)
    if chosen is not None:
        found.update(configured_codec=chosen.encoding, configured_width=chosen.width or None, configured_height=chosen.height or None,
                     configured_fps=chosen.fps or None, configured_bitrate_kbps=chosen.bitrate_kbps or None)
    return found


def _probe_fps(path: Any) -> float | None:
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    try:
        result = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate", "-of", "json", str(path)],
                                capture_output=True, text=True, timeout=15, check=False, env=child_environment())
        rate = json.loads(result.stdout)["streams"][0]["avg_frame_rate"]
        numerator, _, denominator = rate.partition("/")
        value = float(numerator) / float(denominator or 1)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, ZeroDivisionError):
        return None
    return round(value, 2) if value > 0 else None


def measure_recordings(db: sqlite3.Connection, camera_id: str, now: datetime) -> dict[str, Any]:
    """The bitrate of what was recorded in the last half hour (bytes over seconds, from the index) and the frame rate of the newest recording."""
    rows = db.execute("SELECT relative_path,starts_at,ends_at,size_bytes FROM recording_segments WHERE camera_id=? AND ends_at>? AND unavailable_at IS NULL ORDER BY starts_at",
                      (camera_id, _iso(now - MEASURE_WINDOW))).fetchall()
    seconds = sum(max(0.0, (_parse(row["ends_at"]) - _parse(row["starts_at"])).total_seconds()) for row in rows)
    size = sum(row["size_bytes"] or 0 for row in rows)
    result: dict[str, Any] = {"measured_bitrate_kbps": round(size * 8 / 1000 / seconds, 1) if seconds > 0 and size else None, "measured_fps": None}
    if rows:
        from .storage import recording_file_path
        try:
            result["measured_fps"] = _probe_fps(recording_file_path(rows[-1]["relative_path"]))
        except (OSError, ValueError):
            pass
    return result


def _store(db: sqlite3.Connection, camera_id: str, values: dict[str, Any]) -> None:
    db.execute("INSERT OR IGNORE INTO camera_health(camera_id) VALUES(?)", (camera_id,))
    if values:
        db.execute(f"UPDATE camera_health SET {','.join(f'{key}=?' for key in values)} WHERE camera_id=?", (*values.values(), camera_id))


def _store_one(camera_id: str, values: dict[str, Any]) -> None:
    """Store one camera's result on a connection of its own. The write waits for the database's lock, so the pass runs it in a thread: on
    the background worker's event loop it stopped webhook delivery and the worker's heartbeat while it waited."""
    with connect() as db:
        _store(db, camera_id, values)


def note_keyframe_gap(camera_id: str, seconds: float | None) -> None:
    """What the live view saw of a camera's keyframes: the interval between two when it is too long for a viewer to join at once (#496),
    or None when it no longer is. Blocking: the live hub calls it in a thread."""
    try:
        with connect() as db:
            if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is not None:
                _store(db, camera_id, {"keyframe_gap_seconds": seconds, "keyframe_gap_at": _iso(_now())})
    except sqlite3.Error:
        logger.debug("A camera's keyframe interval could not be stored this time", exc_info=True)


def sample_seconds(camera_id: str) -> float:
    """How long after its last check this camera is checked again: SAMPLE_SECONDS moved by an amount of its own, the same every time, in
    [-SPREAD/2, +SPREAD/2). Cameras checked together drift apart and stay apart."""
    import hashlib

    share = int.from_bytes(hashlib.sha256(camera_id.encode()).digest()[:4], "big") / 2**32
    return SAMPLE_SECONDS + (share - 0.5) * SAMPLE_SPREAD_SECONDS


def due(db: sqlite3.Connection, now: datetime, limit: int = 200) -> list[sqlite3.Row]:
    # Read with the earliest any camera can be due; each camera's own time is applied below
    cutoff = _iso(now - timedelta(seconds=SAMPLE_SECONDS - SAMPLE_SPREAD_SECONDS / 2))
    rows = db.execute(
        "SELECT c.*,h.onvif_checked_at,h.measured_at FROM cameras c LEFT JOIN camera_health h ON h.camera_id=c.id WHERE c.archived_at IS NULL "
        "AND ((c.device_service_url IS NOT NULL AND (h.onvif_checked_at IS NULL OR h.onvif_checked_at<?)) OR (c.recording_enabled=1 AND (h.measured_at IS NULL OR h.measured_at<?))) "
        "ORDER BY c.id", (cutoff, cutoff)).fetchall()
    return [row for row in rows if _onvif_due(row, now) or _measure_due(row, now)][:limit]


def _stale(checked_at: str | None, camera_id: str, now: datetime) -> bool:
    return checked_at is None or checked_at < _iso(now - timedelta(seconds=sample_seconds(camera_id)))


def _onvif_due(row: sqlite3.Row, now: datetime) -> bool:
    return bool(row["device_service_url"]) and _stale(row["onvif_checked_at"], row["id"], now)


def _measure_due(row: sqlite3.Row, now: datetime) -> bool:
    return bool(row["recording_enabled"]) and _stale(row["measured_at"], row["id"], now)


async def sample_once(now: datetime | None = None) -> int:
    """Refresh what is stale: the ONVIF report of each ONVIF camera, and the measurements from each recording camera's files. Returns cameras refreshed."""
    from .onvif_client import OnvifError

    now = now or _now()
    with connect() as db:
        cameras = due(db, now)
    gate = asyncio.Semaphore(ONVIF_CONCURRENCY)
    measuring = asyncio.Semaphore(MEASURE_CONCURRENCY)

    async def refresh(row: sqlite3.Row) -> None:
        try:
            await refresh_one(row)
        except Exception:  # noqa: BLE001 - one camera that cannot be sampled must not stop the others, or be retried at once
            logger.exception("The health of camera %s could not be sampled", row["id"])
            await asyncio.to_thread(_store_one, row["id"], {"onvif_checked_at": _iso(now)} if row["device_service_url"] else {})

    async def refresh_one(row: sqlite3.Row) -> None:
        values: dict[str, Any] = {}
        if _onvif_due(row, now):
            values["onvif_checked_at"] = _iso(now)
            try:
                async with gate:
                    reported = await asyncio.wait_for(read_onvif(row), timeout=30)
                values.update(reported, onvif_contact_at=_iso(now), onvif_error=None)
            except OnvifError as exc:
                values["onvif_error"] = exc.message[:300]
            except (asyncio.TimeoutError, OSError, ValueError) as exc:
                values["onvif_error"] = f"The camera did not answer ({type(exc).__name__})"
        if _measure_due(row, now):
            def measure() -> dict[str, Any]:
                with connect() as db:
                    return measure_recordings(db, row["id"], now)
            async with measuring:  # A few at a time, so the worker's other work still finds a thread (#477)
                values.update(await asyncio.to_thread(measure), measured_at=_iso(now))
        await asyncio.to_thread(_store_one, row["id"], values)

    await asyncio.gather(*(refresh(row) for row in cameras))
    return len(cameras)


class HealthManager:
    """Keeps the fleet view's values fresh in the background."""

    def __init__(self, poll_seconds: float = 60.0, initial_delay: float = 30.0) -> None:
        self.poll_seconds, self.initial_delay = poll_seconds, initial_delay  # The first pass waits: starting up is busy enough without a call to every camera
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._run(), name="camera-health")

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def _run(self) -> None:
        await asyncio.sleep(self.initial_delay)
        while True:
            try:
                await sample_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Camera health sampling failed")
            await asyncio.sleep(self.poll_seconds)


# --- the fleet view ---------------------------------------------------------------------------------------------------------------------------

def version_key(version: str) -> tuple[tuple[int, Any], ...]:
    """Firmware versions in natural order: runs of digits compare as numbers, so 2.10 is newer than 2.9, and text after a number sorts below a longer number."""
    tokens = re.findall(r"\d+|[A-Za-z]+", re.sub(r"^\s*[vV](?=\d)", "", version))
    # A release sorts above its own pre-releases (1.0 above 1.0-rc1) and below a later number (1.0.1): the end of the version is a number lower than any real one.
    return tuple((-1, token.lower()) if token.lower() in PRERELEASE else (0, int(token)) if token.isdigit() else (1, token.lower()) for token in tokens) + ((0, -1),)


def firmware_status(rows: list[dict[str, Any]], visible_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Per make and model across all these cameras: the versions in use, the newest, and for each camera whether it is outdated, current or the only version.

    ``rows`` is the whole fleet, so a camera is judged against every camera of its model. ``visible_ids`` are the cameras the reader may see: the newest version is
    named only if one of those runs it (a reader is not told what a camera they cannot see runs), and the summary counts only what they can see."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row["model"] and row["firmware"]:
            groups.setdefault(((row["manufacturer"] or "").strip().lower(), row["model"].strip().lower()), []).append(row)
    models = []
    for members in groups.values():
        versions: dict[str, int] = {}
        for row in members:
            versions[row["firmware"]] = versions.get(row["firmware"], 0) + 1
        newest = max(versions, key=version_key)
        top = version_key(newest)  # Spellings of one version (case, a leading v) are one version
        shown = [row for row in members if visible_ids is None or row["camera_id"] in visible_ids]
        newest_shown = any(version_key(row["firmware"]) == top for row in shown)
        for row in members:
            row["firmware_status"] = "uniform" if len({version_key(version) for version in versions}) == 1 else "current" if version_key(row["firmware"]) == top else "outdated"
            row["newest_firmware"] = newest if row["firmware_status"] == "outdated" and newest_shown else None
        if not shown:
            continue
        seen: dict[str, int] = {}
        for row in shown:
            seen[row["firmware"]] = seen.get(row["firmware"], 0) + 1
        models.append({"manufacturer": shown[0]["manufacturer"], "model": shown[0]["model"], "cameras": len(shown), "newest": newest if newest_shown else None,
                       "firmwares": [{"version": version, "cameras": count} for version, count in sorted(seen.items(), key=lambda item: version_key(item[0]), reverse=True)],
                       "outdated": sum(1 for row in shown if row["firmware_status"] == "outdated")})
    return sorted(models, key=lambda item: (-item["outdated"], (item["model"] or "").lower()))


def fleet(db: sqlite3.Connection, visible: set[str] | None, now: datetime | None = None, *, show_errors: bool = False) -> dict[str, Any]:
    """Every camera the reader may see with its health values; one row per camera, in name order. Reads stored values only. ``show_errors`` adds why the last ONVIF try failed."""
    now = now or _now()
    counters: dict[tuple[str, str], float] = {(row["name"], row["camera_id"]): row["value"] for row in db.execute(
        "SELECT name,camera_id,value FROM metric_counters WHERE name IN (?,?,?)", (RECONNECTS, FRAMES_DROPPED, metrics.SEGMENTS_DROPPED))}
    rows = []
    for camera in db.execute("SELECT c.id,c.name,c.camera_brand,c.recording_status,c.video_codec,c.video_width,c.video_height,g.name AS group_name,h.* "
                             "FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id LEFT JOIN camera_health h ON h.camera_id=c.id "
                             "WHERE c.archived_at IS NULL ORDER BY c.name COLLATE NOCASE,c.id"):
        counted = camera["camera_id"] is not None or any((name, camera["id"]) in counters for name in (RECONNECTS, FRAMES_DROPPED))
        up_since = camera["stream_up_since"] if camera["recording_status"] in UP_STATES else None
        rows.append({
            "camera_id": camera["id"], "name": camera["name"], "group": camera["group_name"], "recording_status": camera["recording_status"],
            "manufacturer": camera["manufacturer"], "model": camera["model"], "firmware": camera["firmware"], "firmware_status": None, "newest_firmware": None,
            "uptime_seconds": round((now - _parse(up_since)).total_seconds()) if up_since else None,
            "configured": {"codec": camera["configured_codec"], "width": camera["configured_width"], "height": camera["configured_height"],
                           "fps": camera["configured_fps"], "bitrate_kbps": camera["configured_bitrate_kbps"]},
            "measured": {"fps": camera["measured_fps"], "bitrate_kbps": camera["measured_bitrate_kbps"], "at": camera["measured_at"]},
            "dropped_frames": int(counters.get((FRAMES_DROPPED, camera["id"]), 0)) if counted else None,
            "reconnects": int(counters.get((RECONNECTS, camera["id"]), 0)) if counted else None,
            "dropped_segments": int(counters.get((metrics.SEGMENTS_DROPPED, camera["id"]), 0)) if counted else None,
            "onvif_contact_at": camera["onvif_contact_at"], "onvif_checked_at": camera["onvif_checked_at"], "onvif_failed": bool(camera["onvif_error"]),
            "onvif_error": camera["onvif_error"] if show_errors else None,  # What the camera answered can name its address: administrators only
            # Keyframes too far apart for instant live view (#496): a viewer who joins waits up to this long for the picture
            "keyframe_gap_seconds": camera["keyframe_gap_seconds"],
        })
    models = firmware_status(rows, visible)
    shown = [row for row in rows if visible is None or row["camera_id"] in visible]
    return {"generated_at": _iso(now), "cameras": shown, "models": models, "outdated": sum(1 for row in shown if row["firmware_status"] == "outdated")}


def _safe(value: Any) -> str:
    text = NOT_REPORTED if value is None or value == "" else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(report: dict[str, Any]) -> str:
    """The fleet as CSV: a value that was not reported says "Not reported"."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(["camera", "group", "manufacturer", "model", "firmware", "firmware_status", "newest_firmware", "stream_uptime_seconds", "configured_bitrate_kbps",
                     "measured_bitrate_kbps", "configured_fps", "measured_fps", "dropped_frames", "reconnects", "last_onvif_contact", "keyframe_gap_seconds"])
    for row in report["cameras"]:
        writer.writerow([_safe(value) for value in (row["name"], row["group"], row["manufacturer"], row["model"], row["firmware"], row["firmware_status"], row["newest_firmware"],
                                                    row["uptime_seconds"], row["configured"]["bitrate_kbps"], row["measured"]["bitrate_kbps"], row["configured"]["fps"],
                                                    row["measured"]["fps"], row["dropped_frames"], row["reconnects"], row["onvif_contact_at"], row["keyframe_gap_seconds"])])
    return out.getvalue()
