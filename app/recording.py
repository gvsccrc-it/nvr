from __future__ import annotations

import asyncio
import random
import csv
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import time
import sqlite3
import uuid
from typing import Any
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .credentials import decrypt
from .processes import end_process
from .store import connect
from .motion import MotionClipAssembler
from .onvif_events import CameraMotionEvent, subscribe_and_pull
from .server_motion import FRAME_HEIGHT, FRAME_WIDTH, FrameMotionDetector
from .scheduling import effective_policy
from . import camera_health, coverage, metrics, security_log, tamper, viewing_audit
from .health import recording_health
from .incidents import sync_incident, sync_storage_incidents

RECONNECT_MAX_SECONDS = 30
# Cameras are started a few at a time, so a restart with many does not launch every FFmpeg, test connection and status write in one pass (#495)
STARTUP_BATCH = 16
STARTUP_BATCH_PAUSE_SECONDS = 0.5


def reconnect_delay(failures: int, rng: random.Random | None = None) -> float:
    """How long a camera waits before it tries again after ``failures`` failures in a row: doubling up to ``RECONNECT_MAX_SECONDS``, with a
    random share (#495). Cameras that drop together (a switch rebooting, the power going) used to retry together, at +2, +4, +8, +16, +30 s
    for the whole outage, every one starting an FFmpeg and a test connection and writing its status in the same second, and all of them
    connecting in the same second when the network came back.

    The first retry is still within 2 to 3 seconds; later ones fall anywhere from half to one and a half times the doubling step, the same
    on average as before."""
    rng = rng or random
    base = min(2 ** min(max(failures, 0), 5), RECONNECT_MAX_SECONDS)
    return base * (rng.uniform(1.0, 1.5) if failures <= 1 else rng.uniform(0.5, 1.5))
RECORDER_GRACE_SECONDS = 5  # How long FFmpeg is given to stop (and finish its last segment) before it is killed; elsewhere 3 s
# An open coverage interval not confirmed for this long means the recorder was not running.
RESTART_GAP_AFTER = timedelta(seconds=15)


def segment_seconds() -> int:
    try:
        configured = int(os.environ.get("NVR_SEGMENT_SECONDS", "60"))
    except ValueError:
        configured = 60
    return max(10, min(configured, 3600))


def recordings_path() -> Path:
    return Path(os.environ.get("NVR_RECORDINGS_PATH", "/recordings")).resolve()


def motion_buffer_path() -> Path:
    return Path(os.environ.get("NVR_MOTION_BUFFER_PATH", "/tmp/nvr-motion-buffer")).resolve()


STREAM_LOG_LINES_PER_MINUTE = 20


async def log_stream_warnings(stream: asyncio.StreamReader, camera_id: str, now=None) -> None:
    """Pass FFmpeg's warnings about the camera's stream (missed RTP packets, timestamps going backwards, a stream that restarted) to the log, a few a minute.

    They were thrown away, so when footage repeated or skipped there was nothing to say why. Only the first lines of each minute are kept and the rest are
    counted, so a camera that is failing does not fill the log; the camera is named, never its address (FFmpeg's lines can contain it, so it is removed).
    """
    import time
    clock = now or time.monotonic
    log = logging.getLogger("nvr.recording")
    window_start, written, held = clock(), 0, 0
    while True:
        line = await stream.readline()
        if not line:
            break
        moment = clock()
        if moment - window_start >= 60:
            if held:
                log.warning("camera %s: %d more FFmpeg stream warnings held back in the last minute", camera_id, held)
            window_start, written, held = moment, 0, 0
        if written >= STREAM_LOG_LINES_PER_MINUTE:
            held += 1
            continue
        written += 1
        text = line.decode("utf-8", "replace").strip()
        log.warning("camera %s: FFmpeg: %s", camera_id, _strip_addresses(text)[:300])
    if held:
        log.warning("camera %s: %d more FFmpeg stream warnings held back", camera_id, held)


def _strip_addresses(text: str) -> str:
    return re.sub(r"(rtsps?|rtp|tcp|udp|https?)://\S+", r"\1://<address>", text)


def safe_stream_url(raw: str) -> str:
    """Validate the only supported ingest protocols and remove userinfo/query from display."""
    parsed = urlsplit(raw.strip())
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise ValueError("Only RTSP and RTSPS camera streams are supported")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid camera stream port")
    if any(ch.isspace() or ch in "'\\\x00" for ch in raw):
        raise ValueError("Camera URL must be URL-encoded and contain no whitespace or quotes")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


async def _stream_fault(source: str) -> str:
    """camera_offline when nothing answers at the camera's stream address, otherwise stream_error."""
    parts = urlsplit(source)
    if not parts.hostname:
        return "stream_error"
    try:
        port = parts.port or {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}.get(parts.scheme, 554)
        _, writer = await asyncio.wait_for(asyncio.open_connection(parts.hostname, port), timeout=3)
    except (OSError, ValueError, asyncio.TimeoutError):
        return "camera_offline"
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return "stream_error"


def _motion_buffer_args(executable: str, spool: Path, row: sqlite3.Row) -> list[str]:
    """FFmpeg's command line for the rolling motion buffer: copy the video (and audio when on) into a short HLS playlist in ``spool``."""
    list_size = min(1024, max(40, int((row["motion_pre_roll_seconds"] + row["motion_post_roll_seconds"] + 60) / 2)))
    return [
        executable, "-hide_banner", "-nostats", "-loglevel", "warning", "-progress", "pipe:1",
        "-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS,
        "-i", "pipe:0", "-map", "0:v:0", *audio_recording_args(bool(row["audio_enabled"])), "-c:v", "copy",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", str(list_size),
        "-hls_flags", "delete_segments+append_list+omit_endlist+program_date_time",
        "-hls_segment_filename", str(spool / "segment-%06d.ts"), str(spool / "live.m3u8"),
    ]


async def _stop_motion_parts(tasks: list["asyncio.Task[None] | None"], assembler: MotionClipAssembler, proc: asyncio.subprocess.Process | None) -> None:
    """Cancel the buffer's helper tasks in order, finish the clip in progress, then end FFmpeg (killing it if it will not stop)."""
    for task in tasks:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await assembler.stop()
    await end_process(proc, RECORDER_GRACE_SECONDS)


def _continuous_args(executable: str, audio: bool, output_dir: Path, list_path: Path, segment_duration: int) -> list[str]:
    """FFmpeg's command line for continuous recording: copy the video (and audio when on) into timestamped MP4 segments, listed in a CSV."""
    return [
        executable, "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
        "-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS,
        "-i", "pipe:0",
        "-map", "0:v:0", *audio_recording_args(audio), "-c:v", "copy", "-f", "segment",
        "-segment_time", str(segment_duration), "-reset_timestamps", "1",
        "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
        "-segment_list", str(list_path), "-segment_list_type", "csv", "-strftime", "1",
        str(output_dir / "segment-%Y%m%dT%H%M%SZ.mp4"),
    ]


def _manifest_bytes(source: str) -> bytes:
    # Feed the source URL to FFmpeg over stdin so camera credentials never appear in argv.
    # RTSP options belong to the nested file opened by the concat demuxer.
    return f"ffconcat version 1.0\nfile {source}\noption rtsp_transport tcp\noption rw_timeout 15000000\n".encode("utf-8")


_PROTOCOLS = "concat,pipe,rtsp,rtsps,tcp,udp,rtp,tls,crypto"


def audio_recording_args(enabled: bool) -> list[str]:
    """Keep audio opt-in and normalize enabled audio to browser-friendly AAC."""
    return ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "96k"] if enabled else ["-an"]


def masked_stream_url(raw: str) -> str:
    parsed = urlsplit(raw)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, "", "", ""))


async def probe_stream(raw: str, timeout_seconds: float = 8.0) -> dict[str, object]:
    source = safe_stream_url(raw)
    executable = shutil.which("ffprobe")
    if not executable:
        return {"reachable": False, "reason": "Media probe is unavailable"}
    args = [
        executable, "-v", "error", "-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS,
        "-show_entries", "stream=codec_type,codec_name,width,height", "-of", "json", "pipe:0",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(_manifest_bytes(source)), timeout=timeout_seconds)
    except (asyncio.TimeoutError, OSError):
        if "proc" in locals() and proc.returncode is None:
            proc.kill()
            await proc.wait()
        return {"reachable": False, "reason": "Camera probe timed out or could not connect"}
    if proc.returncode != 0:
        return {"reachable": False, "reason": "Camera stream could not be opened"}
    try:
        streams = json.loads(stdout).get("streams", [])
    except (json.JSONDecodeError, AttributeError):
        return {"reachable": False, "reason": "Camera returned invalid stream metadata"}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return {"reachable": False, "reason": "Camera stream has no video track"}
    return {
        "reachable": True,
        "audio": any(s.get("codec_type") == "audio" for s in streams),
        "video": {
            "codec": str(video.get("codec_name", "unknown")),
            "width": video.get("width"),
            "height": video.get("height"),
        },
    }


def _record_motion_source(camera_id: str, value: str, reason: str, fallback_enabled: bool) -> None:
    """Store the state of a camera's ONVIF motion events.

    Motion events are one capability and video another: a fault here must not change the video's state (``recording_status``), which follows
    the stream itself. ``last_error`` is set while events are unwell and cleared again only when it is the events' own message."""
    healthy = value == "healthy"
    reason = reason or ("The ONVIF event subscription is unavailable" if value == "unavailable" else "The ONVIF event service is not usable")
    error = None if healthy else f"Motion events: {reason}"
    if error and fallback_enabled:
        error += "; server motion fallback is starting"
    with connect() as db:
        db.execute("UPDATE cameras SET motion_source_status=?,server_motion_status=?,"
                   "last_error=CASE WHEN ? IS NOT NULL THEN ? WHEN last_error LIKE 'Motion events:%' THEN NULL ELSE last_error END WHERE id=?",
                   (value, "waiting" if healthy and fallback_enabled else ("disabled" if not fallback_enabled else "detecting"), error, error, camera_id))


class _MotionSourceWriter:
    """Stores a camera's ONVIF motion-event state in a thread, and only when it changes (#470).

    The event source reports its state after every attempt, every 1 to 30 seconds per camera, and from a plain callback on the recorder's loop.
    Writing each report there made the loop (every camera's watch and the worker's sign of life) wait for the database's lock; with 200 motion
    cameras behind a switch that is down, that was about seven writes a second, each the same as the last. One write runs at a time, always of
    the newest state, so the stored state is never older than the last one reported."""

    def __init__(self, camera_id: str, fallback_enabled: bool) -> None:
        self.camera_id, self.fallback_enabled = camera_id, fallback_enabled
        self.wanted: tuple[str, str] | None = None
        self.written: tuple[str, str] | None = None
        self.task: asyncio.Task[None] | None = None

    def want(self, value: str, reason: str) -> None:
        self.wanted = (value, reason)
        if self.wanted != self.written and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._write(), name=f"motion-source-{self.camera_id}")

    async def _write(self) -> None:
        while self.wanted is not None and self.wanted != self.written:
            wanted = self.wanted
            try:
                await asyncio.to_thread(_record_motion_source, self.camera_id, wanted[0], wanted[1], self.fallback_enabled)
            except sqlite3.Error:
                logging.getLogger("nvr.recording").warning("The motion events' state of camera %s could not be stored", self.camera_id, exc_info=True)
                return  # Tried again at the next report, which is not the one stored
            self.written = wanted

    async def settle(self) -> None:
        """Wait for the write under way, so that a newer state written after this one is never overwritten by it."""
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)


def _camera_exists(camera_id: str) -> bool:
    try:
        with connect() as db:
            return db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is not None
    except sqlite3.Error:
        return True  # Not known: kept, and tried again as when the database cannot be written


def newest_is_fresh(folder: Path, pattern: str, seconds: float = 10) -> bool:
    """Whether the most recently written file matching ``pattern`` was written within ``seconds``.

    FFmpeg, retention and tiering remove files from these folders while they are being listed, so a file that has gone between the listing and
    the look at it is simply not counted (#335): it used to raise, which tore down a motion camera's buffer a few times a day."""
    newest = 0.0
    try:
        for path in folder.glob(pattern):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return False
    return bool(newest) and datetime.now().timestamp() - newest <= seconds


STABLE_SECONDS = 60  # A camera that recorded this long before it dropped starts its reconnect wait from the shortest again (#341)


def failures_after(failures: int, ran_seconds: float) -> int:
    """How many interruptions in a row a camera has had, after one more: a camera that had been recording for a while starts counting again, so
    its next reconnect waits the shortest time. The count used only to grow, and after five interruptions since the recorder started every
    reconnect waited the longest time, however well the camera had recorded in between (#341)."""
    return 1 if ran_seconds >= STABLE_SECONDS else failures + 1


def remove_run_folder_if_empty(folder: Path) -> bool:
    """Remove a recording run's folder that holds no footage: nothing, or only FFmpeg's segment list. Each connection attempt makes a folder
    before FFmpeg starts, so a camera that is offline left one about every 30 seconds, for ever (#341)."""
    try:
        with os.scandir(folder) as entries:
            if any(entry.name != "segments.csv" for entry in entries):
                return False
        shutil.rmtree(folder, ignore_errors=True)
        return True
    except OSError:
        return False


async def _write_camera(sql: str, parameters: tuple[object, ...]) -> None:
    """One write from the recorder's event loop, in a thread: it waits for the database's lock, and while it waits every camera's
    recording supervision and the worker's heartbeat must not."""
    def write() -> None:
        with connect() as db:
            db.execute(sql, parameters)

    await asyncio.to_thread(write)


FRESH_SECONDS = 10.0  # A segment written to within this long is "being written"
RELIST_SECONDS = 6.0  # A run's folder is listed for a newer segment at most this often while its newest one is quiet
CAMERA_WRITE_SECONDS = 5.0  # "Video is arriving" is written for a camera at most this often (#279)
STATUS_REFRESH_SECONDS = 60.0  # An unchanged "recording" state is written again this often, in case anything else has changed it


def _newest_end(db: sqlite3.Connection, camera_id: str) -> str | None:
    """When the camera's newest indexed segment ended. The newest by start is the newest by end: one entry of the (camera, start) index,
    where ``MAX(ends_at)`` walked every entry the camera has (#481)."""
    row = db.execute("SELECT ends_at FROM recording_segments WHERE camera_id=? ORDER BY starts_at DESC LIMIT 1", (camera_id,)).fetchone()
    return row[0] if row else None


RESTARTS_AT_ONCE = 4  # Recordings restarted for changed settings at the same time: a group's volume change touches every camera in it
RESTART_SPACING_SECONDS = 1.0  # Each of them then waits this long before the next takes its place


def _digest(ciphertext: bytes | None) -> str:
    """What an encrypted setting holds, as a digest: saving the same address again makes a new ciphertext, and that is not a change."""
    if not ciphertext:
        return ""
    try:
        return hashlib.sha256(decrypt(ciphertext).encode()).hexdigest()
    except Exception:  # noqa: BLE001 - unreadable: compared as it is stored
        return hashlib.sha256(bytes(ciphertext)).hexdigest()


def recording_inputs(db: sqlite3.Connection, camera_id: str, mode: str) -> tuple[object, ...] | None:
    """Everything a running recording was started with and never reads again: FFmpeg's arguments, the camera's address and login, the
    motion settings and the volume it writes to. A recording whose inputs have changed has to be started again to follow them (#466)."""
    row = db.execute(
        "SELECT c.stream_url_ciphertext,c.audio_enabled,c.motion_service_url,c.motion_credentials_ciphertext,c.motion_pre_roll_seconds,c.motion_post_roll_seconds,"
        "c.motion_fallback_enabled,c.motion_zones_json,c.motion_sensitivity,g.storage_volume_id FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?",
        (camera_id,)).fetchone()
    if row is None:
        return None
    shared = (_digest(row["stream_url_ciphertext"]), bool(row["audio_enabled"]), row["storage_volume_id"] or "default")
    if mode != "motion":
        return shared
    return (*shared, row["motion_service_url"] or "", _digest(row["motion_credentials_ciphertext"]), row["motion_pre_roll_seconds"], row["motion_post_roll_seconds"],
            bool(row["motion_fallback_enabled"]), row["motion_zones_json"], row["motion_sensitivity"])


class RecordingManager:
    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.task_modes: dict[str, str] = {}
        self.task_inputs: dict[str, tuple[object, ...] | None] = {}  # What each running recording was started with (``recording_inputs``)
        self._restart_slots = asyncio.Semaphore(RESTARTS_AT_ONCE)
        # One sync of a camera at a time. Two at once (a settings route's request and the five-second pass) could each stop the recording
        # and each start one: the first of the two new recordings was then lost track of, and ran beside the second until the worker stopped.
        self._sync_locks: dict[str, asyncio.Lock] = {}
        self.scheduler_task: asyncio.Task[None] | None = None
        self.retention_task: asyncio.Task[None] | None = None
        self.headroom_task: asyncio.Task[None] | None = None
        self.tamper = tamper.TamperManager()
        self._dropped: set[str] = set()  # Segments already counted as dropped: the index is re-read every few seconds.
        self._indexed_lines: dict[Path, tuple[int, str]] = {}  # Per FFmpeg segment list: how many lines have been dealt with, and the last of them (#334)
        self.storage_task: asyncio.Task[None] | None = None
        self.stopping = False
        self._database_warned = float("-inf")
        self._run_folders_after = ""  # Where the pass over old run folders carries on
        from . import run_folders

        self._run_folders_round = run_folders.Result()
        self._newest_segment: dict[Path, tuple[str, float]] = {}  # Per run folder: the newest segment's path, and when the folder was last listed
        self._fresh_written: dict[str, float] = {}  # Per camera: when "video is arriving" was last written
        self._status_written: dict[str, tuple[tuple[object, ...], float]] = {}  # Per camera: the last state written, and when
        self._active_runs: set[Path] = set()  # The run folders recordings are writing to now: never touched by the pass over old ones (#484)
        self._unindexed: list[tuple[str, Path, Path]] = []  # Runs that ended while the database could not be written: (camera, list, folder)

    async def start(self) -> None:
        self.stopping = False
        self._mark_restart_gaps()
        with connect() as db:
            viewing_audit.close_orphans(db)
        with connect() as db:
            rows = db.execute("SELECT id FROM cameras").fetchall()
        try:  # Before any camera starts: a run's folder is empty at the moment it is made
            await asyncio.to_thread(self._sweep_empty_run_folders, [row["id"] for row in rows])
        except Exception:  # noqa: BLE001 - tidying must never stop recording from starting
            logging.getLogger("nvr.recording").exception("Empty recording folders could not be cleared")
        for number, row in enumerate(rows):
            if number and number % STARTUP_BATCH == 0:
                await asyncio.sleep(STARTUP_BATCH_PAUSE_SECONDS)  # A few at a time (#495)
            await self.sync_camera(row["id"])
        self.scheduler_task = asyncio.create_task(self._schedule_loop(), name="recording-schedule-supervisor")
        self.retention_task = asyncio.create_task(self._retention_loop(), name="recording-retention-cleanup")
        self.headroom_task = asyncio.create_task(self._headroom_loop(), name="recording-storage-headroom")
        self.storage_task = asyncio.create_task(self._storage_loop(), name="recording-storage-health")
        await self.tamper.start()

    @staticmethod
    def _sweep_empty_run_folders(camera_ids: list[str]) -> int:
        """Clear the empty run folders left by earlier connection attempts and by runs whose footage has all expired."""
        from .storage import camera_recordings_dir

        removed = 0
        for camera_id in camera_ids:
            try:
                with os.scandir(camera_recordings_dir(camera_id)) as runs:
                    folders = [Path(entry.path) for entry in runs if entry.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            removed += sum(remove_run_folder_if_empty(folder) for folder in folders)
        return removed

    async def stop(self) -> None:
        self.stopping = True
        await self.tamper.stop()
        if self.scheduler_task and not self.scheduler_task.done():
            self.scheduler_task.cancel()
            await asyncio.gather(self.scheduler_task, return_exceptions=True)
        self.scheduler_task = None
        if self.retention_task and not self.retention_task.done():
            self.retention_task.cancel()
            await asyncio.gather(self.retention_task, return_exceptions=True)
        self.retention_task = None
        if self.headroom_task and not self.headroom_task.done():
            self.headroom_task.cancel()
            await asyncio.gather(self.headroom_task, return_exceptions=True)
        self.headroom_task = None
        if self.storage_task and not self.storage_task.done():
            self.storage_task.cancel()
            await asyncio.gather(self.storage_task, return_exceptions=True)
        self.storage_task = None
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.task_modes.clear()

    async def remove(self, camera_id: str) -> None:
        task = self.tasks.pop(camera_id, None)
        self.task_modes.pop(camera_id, None)
        self.task_inputs.pop(camera_id, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _wanted(self, camera_id: str) -> tuple[str, dict[str, Any], sqlite3.Row | None, tuple[object, ...] | None]:
        """The mode the camera should be recorded in now, and what such a recording would be started with."""
        with connect() as db:
            policy = effective_policy(db, camera_id, datetime.now(timezone.utc))
            row = db.execute("SELECT enabled,recording_enabled,recording_status,archived_at FROM cameras WHERE id=?", (camera_id,)).fetchone()
            desired = policy["mode"] if row and row["enabled"] and row["recording_enabled"] and not row["archived_at"] else "disabled"
            inputs = recording_inputs(db, camera_id, desired) if desired in {"motion", "continuous"} else None
        return desired, policy, row, inputs

    def _settled(self, camera_id: str, desired: str, inputs: tuple[object, ...] | None, force_restart: bool) -> tuple[bool, bool]:
        """Whether the camera's task is already what is wanted, and whether it is a running recording whose settings have changed."""
        task = self.tasks.get(camera_id)
        if task is None or self.task_modes.get(camera_id) != desired:
            return False, False
        failed = task.done() and not task.cancelled() and task.exception() is not None
        changed = not task.done() and self.task_inputs.get(camera_id) != inputs
        return not failed and not changed and (not task.done() or not force_restart), changed

    async def sync_camera(self, camera_id: str, force_restart: bool = False) -> None:
        """Start, stop or restart a camera's recording to match its settings and schedule.

        A recording that is running in the right mode is left alone unless what it was started with has changed (audio, the address or login,
        the motion settings, the volume): then it is started again, whether or not the caller asked, because the running FFmpeg would
        otherwise keep its old arguments until the stream next dropped (#466). ``force_restart`` restarts nothing by itself: a schedule
        save for a group asks it of every camera, and must not cost each of them a gap. Recordings restarted for changed settings go a few
        at a time."""
        async with self._sync_locks.setdefault(camera_id, asyncio.Lock()):
            desired, policy, row, inputs = self._wanted(camera_id)
            settled, changed = self._settled(camera_id, desired, inputs, force_restart)
            if settled:
                return
            if not changed:
                await self._replace(camera_id, desired, policy, row, inputs)
                return
            async with self._restart_slots:
                desired, policy, row, inputs = self._wanted(camera_id)  # Read again: it may have changed once more while this waited its turn
                await self._replace(camera_id, desired, policy, row, inputs)
                await asyncio.to_thread(self._note_settings_restart, camera_id)
                await asyncio.sleep(RESTART_SPACING_SECONDS)

    async def _replace(self, camera_id: str, desired: str, policy: dict[str, Any], row: sqlite3.Row | None, inputs: tuple[object, ...] | None) -> None:
        await self.remove(camera_id)
        if desired in {"motion", "continuous"} and not self.stopping:
            self.task_modes[camera_id] = desired
            self.task_inputs[camera_id] = inputs
            self.tasks[camera_id] = asyncio.create_task(self._run(camera_id, desired), name=f"record-{camera_id}")
        elif row and row["enabled"] and row["recording_enabled"] and policy["scheduled"]:
            if row["recording_status"] != "scheduled_idle":
                await self._status(camera_id, "scheduled_idle")
        elif row and not row["archived_at"] and row["recording_status"] != "stopped":
            await self._status(camera_id, "stopped")

    @staticmethod
    def _note_settings_restart(camera_id: str) -> None:
        """The few seconds without footage while a recording starts again are a gap with a reason of their own. The coverage check that
        runs every five seconds closes it when footage is being written again."""
        now = datetime.now(timezone.utc).isoformat()
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("UPDATE coverage_intervals SET ends_at=? WHERE camera_id=? AND ends_at IS NULL", (now, camera_id))
                db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at,checked_at) VALUES(?,?,?,?,?,?)",
                           (camera_id, "uncertain", "The recording was started again to apply changed settings", "settings_restart", now, now))
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    async def _schedule_loop(self) -> None:
        while not self.stopping:
            try:
                with connect() as db:
                    rows = db.execute("SELECT id FROM cameras").fetchall()
                # A recording whose camera's row has gone (deleted by another process, or by a standby's sync of the primary's cameras) is
                # synced too, which stops it: otherwise it recorded for good, and its footage was indexed onto any later camera of that id.
                gone = [camera_id for camera_id in self.tasks if camera_id not in {row["id"] for row in rows}]
                await asyncio.gather(*(self.sync_camera(camera_id) for camera_id in [row["id"] for row in rows] + gone))
                # Each camera's coverage check writes, and a write waits for the database's lock: in a thread, as the watch does.
                await asyncio.to_thread(self._sync_all_coverage, [row["id"] for row in rows])
                if self._unindexed:
                    await asyncio.to_thread(self._index_leftovers)
            except asyncio.CancelledError:
                raise
            except sqlite3.Error:
                pass
            await asyncio.sleep(5)

    async def _headroom_loop(self) -> None:
        """Keep each volume's free-space headroom by deleting its oldest footage, often enough that a busy site cannot fill the disk between passes."""
        from .headroom import keep_headroom
        while not self.stopping:
            try:
                await asyncio.to_thread(keep_headroom)
            except asyncio.CancelledError:
                raise
            except (OSError, sqlite3.Error):
                pass
            await asyncio.sleep(60)

    async def _retention_loop(self) -> None:
        from .retention import cleanup_expired_recordings
        started = False
        while not self.stopping:
            try:
                await asyncio.to_thread(cleanup_expired_recordings)
                await asyncio.to_thread(self._prune_viewing_audit)
                await asyncio.to_thread(self._reconcile_run_folders)
                await asyncio.to_thread(self._sweep_track_pictures)
                if started:  # Not at startup: the disks are read from the second pass, five minutes in, so starting up runs no external command.
                    await asyncio.to_thread(self._watch_disks)
            except asyncio.CancelledError:
                raise
            except (OSError, sqlite3.Error):
                pass
            started = True
            await asyncio.sleep(300)

    @staticmethod
    def _sweep_track_pictures() -> None:
        """Delete the picture files of objects whose detections have expired (#512), a bounded number a step, until none is left to do now."""
        from . import track_pictures

        for _ in range(20):
            if track_pictures.sweep()["removed"] < track_pictures.SWEEP_BATCH:
                break

    def _reconcile_run_folders(self) -> None:
        """Go over the run folders no recording is using: index what an earlier recorder finished and did not index, remove the segment it
        was writing when it was killed, and count what is on disk without a row (#484). A bounded number of folders a pass."""
        from . import run_folders

        try:
            with connect() as db:
                camera_ids = [row[0] for row in db.execute("SELECT id FROM cameras")]
            waiting = {output_dir for _, _, output_dir in self._unindexed}  # Theirs to index when the database allows (#483)
            result, self._run_folders_after = run_folders.reconcile(self, camera_ids, set(self._active_runs) | waiting, after=self._run_folders_after)
            for name in ("folders", "indexed", "removed_files", "removed_bytes", "unindexed_files", "unindexed_bytes"):
                setattr(self._run_folders_round, name, getattr(self._run_folders_round, name) + getattr(result, name))
            if not self._run_folders_after:  # Every folder has been seen: this round's count is the whole disk's
                run_folders.record(self._run_folders_round)
                self._run_folders_round = run_folders.Result()
        except Exception:  # noqa: BLE001 - never stops the retention pass that shares this loop
            logging.getLogger("nvr.recording").exception("The pass over old run folders failed")

    @staticmethod
    def _watch_disks() -> None:
        """The retention forecast every pass, and the disks' SMART health when it is due: an incident for each failing disk or camera that will not keep its policy."""
        from . import forecast, smart
        try:
            volumes = smart.volumes_to_read()
            with connect() as db:
                forecast.sync_incidents(db, forecast.refresh(db))  # Worked out here and kept: the scrape and the forecast pages read it (#460)
                due = smart.due(db, volume_ids=volumes)
            if due:
                smart.refresh(volumes)
        except Exception:  # noqa: BLE001 - watching the disks must never stop the retention pass that shares this loop
            logging.getLogger("nvr.recording").exception("The disk watch failed")

    @staticmethod
    def _prune_viewing_audit() -> None:
        with connect() as db:
            viewing_audit.close_orphans(db)  # What a crash left open, once it has gone quiet; at start alone, a full restart's own were too fresh to close
            viewing_audit.prune(db)
            from . import backfill  # Here: backfill imports this module

            backfill.prune_jobs(db)  # Jobs about time older than the camera's oldest footage (#475)
            security_log.prune(db)
        from . import talk

        talk.clear_old_recordings()  # Kept talk audio goes after 30 days whether or not anyone talks again (#478)

    async def _storage_loop(self) -> None:
        while not self.stopping:
            try:
                await asyncio.to_thread(self._sync_storage_health)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, sqlite3.Error):
                pass
            await asyncio.sleep(30)

    @staticmethod
    def _sync_storage_health() -> None:
        from .storage import storage_status
        status = storage_status()
        with connect() as db:
            sync_storage_incidents(db, status["volumes"])

    @staticmethod
    def _mark_restart_gaps(now: datetime | None = None) -> None:
        """While the recorder was not running nothing was recorded: close each open interval when it was last
        confirmed and record the time since as a worker_restart gap. An interval where no recording was expected stays open."""
        now = now or datetime.now(timezone.utc)
        with connect() as db:
            for interval in db.execute("SELECT * FROM coverage_intervals WHERE ends_at IS NULL").fetchall():
                checked = interval["checked_at"] or interval["starts_at"]
                if now - datetime.fromisoformat(checked.replace("Z", "+00:00")) < RESTART_GAP_AFTER:
                    continue
                if interval["coverage_kind"] == "expected_idle":
                    continue  # Nothing was expected: the interval carries on across the restart.
                db.execute("UPDATE coverage_intervals SET ends_at=? WHERE id=?", (checked, interval["id"]))
                db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at,ends_at,checked_at) VALUES(?,?,?,?,?,?,?)",
                           (interval["camera_id"], "failure", "The recorder was not running", "worker_restart", checked, now.isoformat(), now.isoformat()))

    def _sync_all_coverage(self, camera_ids: list[str]) -> None:
        for camera_id in camera_ids:
            self._sync_coverage(camera_id)

    def _sync_coverage(self, camera_id: str) -> None:
        with connect() as db:
            row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
            if row is None:
                return
            policy = effective_policy(db, camera_id, datetime.now(timezone.utc))
            health = recording_health(
                enabled=bool(row["enabled"]), recording_enabled=bool(row["recording_enabled"]),
                mode=row["recording_mode"], effective_mode=str(policy["mode"]),
                recording_status=row["recording_status"], motion_source_status=row["motion_source_status"],
                last_video_at=row["last_video_at"], last_footage_write_at=row["last_footage_write_at"],
            )
            storage_faulty = db.execute(
                "SELECT 1 FROM incidents i, json_each(i.details_json, '$.affected_cameras') c "
                "WHERE i.fault_type='storage' AND i.fault_active=1 AND json_extract(c.value, '$.id')=? LIMIT 1", (camera_id,)).fetchone() is not None
            if health["video_fresh"] and row["stream_fault"]:
                db.execute("UPDATE cameras SET stream_fault=NULL WHERE id=?", (camera_id,))  # Fresh video: the stream works again.
                row = db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
            code = coverage.reason_code(row, policy, health, storage_faulty)
            active = db.execute("SELECT id,coverage_kind,reason_code FROM coverage_intervals WHERE camera_id=? AND ends_at IS NULL ORDER BY id DESC LIMIT 1", (camera_id,)).fetchone()
            kind = str(health["coverage_kind"])
            now = datetime.now(timezone.utc).isoformat()
            if active and active["coverage_kind"] == kind and active["reason_code"] == code:
                db.execute("UPDATE coverage_intervals SET reason=?,checked_at=? WHERE id=?", (str(health["detail"]), now, active["id"]))
            else:
                if active:
                    db.execute("UPDATE coverage_intervals SET ends_at=? WHERE id=?", (now, active["id"]))
                db.execute("INSERT INTO coverage_intervals(camera_id,coverage_kind,reason,reason_code,starts_at,checked_at) VALUES(?,?,?,?,?,?)",
                           (camera_id, kind, str(health["detail"]), code, now, now))
            for fault_type, coverage_kinds, title in (
                ("recording_failure", {"failure"}, "Recording coverage failure"),
                ("recording_uncertain", {"uncertain"}, "Recording coverage is uncertain"),
            ):
                sync_incident(
                    db,
                    incident_key=f"{fault_type}:{camera_id}",
                    camera_id=camera_id,
                    fault_type=fault_type,
                    summary=f"{title}: {row['name']} — {health['detail']}",
                    active=kind in coverage_kinds,
                    details={"coverage_kind": kind, "reason": str(health["detail"])},
                )

    def _desired_mode(self, camera_id: str) -> str:
        with connect() as db:
            return str(effective_policy(db, camera_id, datetime.now(timezone.utc))["mode"])

    def _camera(self, camera_id: str) -> sqlite3.Row | None:
        with connect() as db:
            return db.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()

    async def _status(self, camera_id: str, value: str, error: str | None = None, fault: str | None = None) -> None:
        """``_set_status`` from the event loop: in a thread, since the write waits for the database's lock, and while it waits every
        camera's recording supervision and the worker's heartbeat must not. A burst of these comes when many cameras drop at once."""
        await asyncio.to_thread(self._set_status, camera_id, value, error, fault)

    def _set_status(self, camera_id: str, value: str, error: str | None = None, fault: str | None = None, *, unless_written_within: float = 0.0) -> None:
        """Record the recorder's state. ``fault`` says what went wrong when the state is a failure; it is kept while the recorder
        retries and cleared once the stream works (see ``_sync_coverage``) or recording stops. ``unless_written_within``: nothing is
        written when this same state was written that recently (the watch says "recording" every two seconds for as long as it is)."""
        now = time.monotonic()
        written = self._status_written.get(camera_id)
        if unless_written_within and written is not None and written[0] == (value, error, fault) and now - written[1] < unless_written_within:
            return
        self._status_written[camera_id] = ((value, error, fault), now)
        with connect() as db:
            db.execute(
                "UPDATE cameras SET recording_status=?, last_error=?, "
                "stream_fault=CASE WHEN ? IN ('reconnecting','unavailable','failed') THEN ? WHEN ? IN ('recording','stopped','scheduled_idle') THEN NULL ELSE stream_fault END, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (value, error, value, fault, value, camera_id),
            )

    def _mark_video_fresh(self, camera_id: str, at_most_every: float = 0.0) -> None:
        """Say that video is arriving now. ``at_most_every``: skipped when it was said that recently (the watch asks every two seconds;
        "fresh" means within 45 s, and one write per camera every five seconds was the owner's choice on #279)."""
        now = time.monotonic()
        if at_most_every and now - self._fresh_written.get(camera_id, float("-inf")) < at_most_every:
            return
        self._fresh_written[camera_id] = now
        with connect() as db:
            db.execute("UPDATE cameras SET last_video_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (camera_id,))

    def _count_dropped(self, camera_id: str, name: str) -> None:
        if (key := f"{camera_id}/{name}") not in self._dropped:
            if len(self._dropped) >= 10_000:
                self._dropped.clear()
            self._dropped.add(key)
            metrics.segment_dropped(camera_id)

    def _index_csv(self, camera_id: str, index_path: Path, output_dir: Path, *, must_store: bool = False) -> int:
        """Index the segments FFmpeg has finished since the last pass over this list, and return how many were new.

        FFmpeg adds a line per finished segment and never trims the list, so the lines already dealt with are remembered per list: a pass costs
        what is new, not how long the camera has been connected (#334). A pass with nothing new touches no database; new segments and their
        counters are stored in one transaction, and if that fails nothing is remembered, so the same lines are tried again next pass
        (``must_store``: the failure is raised, for the last pass over a run's list, which has no next pass of its own)."""
        # Where the last pass got to in the list, in bytes, and the line that ended there: the list is read from that point, not from its
        # start. FFmpeg never trims it, so after a month connected it holds 43,000 lines, and reading them all every two seconds for
        # every camera was a part of what the watch cost that grew without end (#481).
        done, last = self._indexed_lines.get(index_path, (0, ""))
        try:
            with index_path.open("rb") as source:
                if done:
                    remembered = last.encode("utf-8")
                    source.seek(max(0, done - len(remembered)))
                    if source.read(len(remembered)) != remembered:  # Not the list that was remembered: start again
                        done = 0
                        source.seek(0)
                lines = source.read().decode("utf-8").splitlines(keepends=True)
        except (OSError, UnicodeError):
            return 0
        if not lines:
            return 0
        # A last line without its line end may still be being written: it is read, and read again next pass, never remembered.
        finished = lines if lines[-1].endswith(("\n", "\r")) else lines[:-1]
        try:
            rows = list(csv.reader(lines))
        except csv.Error:
            return 0
        root = recordings_path()
        found: list[tuple[str, datetime, datetime, int]] = []
        for row in rows:
            if len(row) < 3:
                continue
            candidate = Path(row[0])
            if candidate.is_absolute() or candidate.name != row[0] or candidate.suffix.lower() != ".mp4":
                continue
            media = (output_dir / candidate.name).resolve()
            try:
                size = media.stat().st_size if root in media.parents and media.is_file() else None
            except OSError:  # Removed between the two looks (retention): gone, not lost
                size = None
            if size is None or size < 128:
                # A file that is there but too small to be video was dropped; one that is gone was removed (retention), not lost.
                if size is not None:
                    self._count_dropped(camera_id, candidate.name)
                continue
            try:
                duration = max(0.0, float(row[2]) - float(row[1]))
                started = datetime.strptime(candidate.stem, "segment-%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                self._count_dropped(camera_id, candidate.name)
                continue
            found.append((media.relative_to(root).as_posix(), started, started + timedelta(seconds=duration), size))
        indexed = 0
        if found:
            try:
                with connect() as db:
                    db.execute("BEGIN IMMEDIATE")  # The segments and their counters are one change.
                    try:
                        for relative, started, ended, size in found:
                            cur = db.execute(
                                "INSERT OR IGNORE INTO recording_segments(camera_id, relative_path, starts_at, ends_at, size_bytes) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (camera_id, relative, started.isoformat(), ended.isoformat(), size),
                            )
                            if cur.rowcount:
                                metrics.segment_written(db, camera_id, ended)
                                indexed += 1
                        if indexed:
                            db.execute("UPDATE cameras SET last_footage_write_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),recording_fault_since=NULL WHERE id=?", (camera_id,))
                    except BaseException:
                        db.execute("ROLLBACK")
                        raise
                    db.execute("COMMIT")
            except sqlite3.Error:
                if must_store:
                    raise
                return 0  # Nothing is remembered: these lines are read again next pass
        if finished:
            self._indexed_lines[index_path] = (done + sum(len(line.encode("utf-8")) for line in finished), finished[-1])
        return indexed

    async def _run(self, camera_id: str, expected_mode: str) -> None:
        """A camera's recording task. An error inside it (the database busy for longer than its timeout, a file gone) is logged and the camera is
        tried again after a wait: it used to end the task, and the camera then stayed unrecorded until its settings changed (#335)."""
        errors = 0
        while not self.stopping:
            try:
                await self._run_once(camera_id, expected_mode)
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - see the docstring
                errors += 1
                logging.getLogger("nvr.recording").exception("The recording task for camera %s failed; starting it again", camera_id)
                await asyncio.sleep(reconnect_delay(errors))

    async def _run_once(self, camera_id: str, expected_mode: str) -> None:
        failures = 0
        while not self.stopping:
            row = self._camera(camera_id)
            if row is None or not row["enabled"] or not row["recording_enabled"] or self._desired_mode(camera_id) != expected_mode:
                await self._status(camera_id, "stopped")
                return
            if expected_mode == "motion":
                began = time.monotonic()
                should_retry = await self._run_motion(camera_id)
                ran_for = time.monotonic() - began
                row = self._camera(camera_id)
                if row is None or not row["enabled"] or not row["recording_enabled"] or self._desired_mode(camera_id) != expected_mode or self.stopping:
                    return
                if not should_retry:
                    return
                failures = failures_after(failures, ran_for)
                await asyncio.sleep(reconnect_delay(failures))
            else:
                await self._run_continuous(camera_id, expected_mode)
                return

    def _motion_inputs(self, camera_id: str, row: sqlite3.Row) -> tuple[str, str, dict[str, str]] | None:
        """FFmpeg, the stream address and the event credentials; None once the camera's status says why it cannot start."""
        fallback_enabled = bool(row["motion_fallback_enabled"])
        if not row["motion_service_url"] and not fallback_enabled:
            with connect() as db:
                db.execute("UPDATE cameras SET recording_status='unsupported',motion_source_status='unsupported',server_motion_status='disabled',last_error='Configure an ONVIF event service or enable server motion fallback' WHERE id=?", (camera_id,))
            return None
        executable = shutil.which("ffmpeg")
        if not executable:
            with connect() as db:
                db.execute("UPDATE cameras SET recording_status='unavailable',motion_source_status='unavailable',server_motion_status=?,last_error='FFmpeg is not installed' WHERE id=?", ("unavailable" if fallback_enabled else "disabled", camera_id))
            return None
        try:
            source = decrypt(row["stream_url_ciphertext"])
            safe_stream_url(source)
            credentials = json.loads(decrypt(row["motion_credentials_ciphertext"])) if row["motion_credentials_ciphertext"] else {"username": "", "password": ""}
        except Exception:
            with connect() as db:
                db.execute("UPDATE cameras SET recording_status='failed',motion_source_status='configuration_error',last_error='Camera credentials are unavailable' WHERE id=?", (camera_id,))
            return None
        return executable, source, credentials

    async def _run_motion(self, camera_id: str) -> bool:
        row = self._camera(camera_id)
        if row is None or not row["enabled"] or not row["recording_enabled"]:
            await self._status(camera_id, "stopped")
            return False
        fallback_enabled = bool(row["motion_fallback_enabled"])
        inputs = await asyncio.to_thread(self._motion_inputs, camera_id, row)  # It writes the camera's status when it cannot start
        if inputs is None:
            return False
        executable, source, credentials = inputs

        spool = motion_buffer_path() / camera_id
        spool.mkdir(parents=True, exist_ok=True)
        assembler = MotionClipAssembler(camera_id, spool, row["motion_pre_roll_seconds"], row["motion_post_roll_seconds"])
        await self._status(camera_id, "buffering")
        await _write_camera("UPDATE cameras SET motion_source_status=?,server_motion_status=?,last_error=NULL WHERE id=?",
                            ("connecting" if row["motion_service_url"] else "unsupported", "waiting" if fallback_enabled else "disabled", camera_id))
        event_task: asyncio.Task[None] | None = None
        progress_task: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[None] | None = None
        fallback_task: asyncio.Task[None] | None = None
        proc: asyncio.subprocess.Process | None = None
        source_state = {"value": "connecting" if row["motion_service_url"] else "unsupported"}
        source_changed = asyncio.Event()
        source_writer = _MotionSourceWriter(camera_id, fallback_enabled)
        try:
            args = _motion_buffer_args(executable, spool, row)
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            progress_task = asyncio.create_task(camera_health.watch_progress(proc.stdout, camera_id), name=f"progress-{camera_id}")
            stderr_task = asyncio.create_task(log_stream_warnings(proc.stderr, camera_id), name=f"ffmpeg-log-{camera_id}")
            if proc.stdin is None:
                raise OSError("Unable to pass stream source to FFmpeg")
            proc.stdin.write(_manifest_bytes(source))
            await proc.stdin.drain()
            proc.stdin.close()

            problem = {"text": ""}

            def source_status(value: str) -> None:
                source_state["value"] = value
                source_changed.set()
                source_writer.want(value, problem["text"])

            def source_detail(text: str) -> None:
                problem["text"] = text

            async def on_event(event) -> None:
                await assembler.ingest(event)

            if row["motion_service_url"]:
                event_task = asyncio.create_task(
                    subscribe_and_pull(row["motion_service_url"], credentials.get("username", ""), credentials.get("password", ""), on_event, source_status, on_detail=source_detail),
                    name=f"onvif-events-{camera_id}",
                )
            else:
                source_status("unsupported")
            if fallback_enabled:
                fallback_task = asyncio.create_task(
                    self._watch_server_motion_fallback(
                        camera_id, source, json.loads(row["motion_zones_json"]), row["motion_sensitivity"],
                        source_state, source_changed, assembler,
                    ),
                    name=f"server-motion-{camera_id}",
                )
            await self._watch_motion_buffer(camera_id, proc, spool, assembler, source)
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError):
            fault = await _stream_fault(source)
            await _write_camera("UPDATE cameras SET recording_status='reconnecting',motion_source_status='unavailable',last_error='Camera motion buffer could not start',stream_fault=? WHERE id=?",
                                (fault, camera_id))
        finally:
            await _stop_motion_parts([progress_task, stderr_task, event_task, fallback_task], assembler, proc)
            await source_writer.settle()  # Before the next start writes "connecting", which this one's last write must not overwrite
            if self.stopping:
                self._set_status(camera_id, "stopped")
            shutil.rmtree(spool, ignore_errors=True)
        return True

    async def _watch_motion_buffer(self, camera_id: str, proc: asyncio.subprocess.Process, spool: Path, assembler: MotionClipAssembler, source: str) -> None:
        """Poll the clip assembler once a second until the manager stops or FFmpeg exits; a fresh segment marks the video live."""
        while not self.stopping:
            await assembler.poll()
            # A write waits for the database's lock: in a thread, so that a wait never stops every camera's recording and the heartbeat.
            try:
                if await asyncio.to_thread(newest_is_fresh, spool, "segment-*.ts"):
                    await asyncio.to_thread(self._mark_video_fresh, camera_id)
            except sqlite3.Error as exc:
                self._database_unavailable(camera_id, exc)  # Not written this time; the buffer is filling normally and is left running (#483)
            if proc.returncode is not None:
                fault = await _stream_fault(source)
                try:
                    await asyncio.to_thread(self._set_status, camera_id, "reconnecting", "Camera stream buffer interrupted; retrying", fault=fault)
                except sqlite3.Error as exc:
                    self._database_unavailable(camera_id, exc)
                break
            await asyncio.sleep(1)

    async def _watch_server_motion_fallback(
        self,
        camera_id: str,
        source: str,
        zones: list[dict[str, float]],
        sensitivity: int,
        source_state: dict[str, str],
        source_changed: asyncio.Event,
        assembler: MotionClipAssembler,
    ) -> None:
        while not self.stopping:
            if source_state["value"] in {"healthy", "connecting"}:
                await _write_camera("UPDATE cameras SET server_motion_status='waiting' WHERE id=?", (camera_id,))
                source_changed.clear()
                if source_state["value"] in {"healthy", "connecting"}:
                    await source_changed.wait()
                continue
            try:
                await self._detect_server_motion(camera_id, source, zones, sensitivity, source_state, assembler)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, asyncio.IncompleteReadError, TimeoutError):
                if source_state["value"] in {"healthy", "connecting"}:
                    await _write_camera("UPDATE cameras SET server_motion_status='waiting' WHERE id=?", (camera_id,))
                else:
                    await _write_camera("UPDATE cameras SET server_motion_status='unavailable',recording_status='unavailable',last_error='ONVIF events and server motion fallback are unavailable' WHERE id=?", (camera_id,))
            if source_state["value"] not in {"healthy", "connecting"} and not self.stopping:
                await asyncio.sleep(2)

    async def _detect_server_motion(
        self,
        camera_id: str,
        source: str,
        zones: list[dict[str, float]],
        sensitivity: int,
        source_state: dict[str, str],
        assembler: MotionClipAssembler,
    ) -> None:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise OSError("FFmpeg is not installed")
        detector = FrameMotionDetector(zones, sensitivity)
        args = [
            executable, "-hide_banner", "-nostats", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-protocol_whitelist", _PROTOCOLS,
            "-i", "pipe:0", "-map", "0:v:0", "-vf", f"fps=2,scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=area,format=gray",
            "-an", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
        ]
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            if process.stdin is None or process.stdout is None:
                raise OSError("Unable to start server motion detector")
            process.stdin.write(_manifest_bytes(source))
            await process.stdin.drain()
            process.stdin.close()
            await _write_camera("UPDATE cameras SET server_motion_status='detecting',recording_status='buffering',last_error='ONVIF events are degraded; server motion fallback is active' WHERE id=?", (camera_id,))
            frame_size = FRAME_WIDTH * FRAME_HEIGHT
            while not self.stopping and source_state["value"] not in {"healthy", "connecting"}:
                frame = await asyncio.wait_for(process.stdout.readexactly(frame_size), timeout=5)
                transition = detector.process(frame)
                if transition is not None:
                    await assembler.ingest(CameraMotionEvent(
                        transition, datetime.now(timezone.utc), "server-motion-difference", source="server",
                    ))
                if process.returncode is not None:
                    raise OSError("Server motion detector stopped")
        finally:
            if detector.active:
                await assembler.ingest(CameraMotionEvent(
                    False, datetime.now(timezone.utc), "server-motion-difference", source="server",
                ))
            await end_process(process, RECORDER_GRACE_SECONDS)
            if source_state["value"] in {"healthy", "connecting"}:
                with connect() as db:
                    db.execute("UPDATE cameras SET server_motion_status='waiting' WHERE id=?", (camera_id,))

    def _continuous_source(self, camera_id: str, row: sqlite3.Row) -> str | None:
        """The camera's stream address, or None once the camera's status says its credentials or address cannot be used."""
        try:
            source = decrypt(row["stream_url_ciphertext"])
            safe_stream_url(source)
        except Exception:
            self._set_status(camera_id, "failed", "Camera credentials are unavailable", fault="stream_error")
            return None
        return source

    async def _end_ffmpeg(self, process: asyncio.subprocess.Process | None) -> None:
        """Ask FFmpeg to stop, and kill it if it has not within five seconds."""
        await end_process(process, RECORDER_GRACE_SECONDS)

    async def _watch_continuous_output(self, camera_id: str, process: asyncio.subprocess.Process, list_path: Path, output_dir: Path, segment_duration: int) -> None:
        """Every two seconds while FFmpeg runs: index the finished segments, mark the video fresh when a file is being written, and say whether the
        recording is current ("recording") or has stalled (the start of the fault is kept)."""
        while process.returncode is None and not self.stopping:
            # Its writes wait for the database's lock: in a thread, so that a wait never stops every camera's recording and the heartbeat.
            try:
                await asyncio.to_thread(self._watch_pass, camera_id, list_path, output_dir, segment_duration)
            except sqlite3.Error as exc:
                # The database could not be read or written this time. FFmpeg is recording normally: ending it here, as this used to, cost
                # every camera a gap at the same moment for a ten-second stall of the one database they share (#483). The segments finished
                # meanwhile are indexed by the next pass that gets through.
                self._database_unavailable(camera_id, exc)
            await asyncio.sleep(2)

    def _database_unavailable(self, camera_id: str, error: sqlite3.Error) -> None:
        """Say that a recording's status could not be written, once a minute for the whole recorder and not once a pass per camera."""
        from . import db_health

        log = logging.getLogger("nvr.recording")
        if db_health.is_damage(error):
            log.error("The recording of camera %s could not be indexed", camera_id, exc_info=error)  # The damage filter makes this one clear line (#453)
            return
        now = time.monotonic()
        if now - self._database_warned >= 60:
            self._database_warned = now
            log.warning("The database could not be written (%s): recording carries on, and the footage is indexed when it can be (camera %s, and any others)", error, camera_id)

    def _index_rest(self, camera_id: str, list_path: Path, output_dir: Path) -> None:
        """Index what is left of a run's list as the run ends, whichever way it ends. If the database still cannot be written the list is
        kept and tried again every few seconds: the run's folder is never read again otherwise, and its last segments stayed on disk with
        no row (#483)."""
        try:
            self._index_csv(camera_id, list_path, output_dir, must_store=True)
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.IntegrityError) and not _camera_exists(camera_id):
                # The camera was deleted: its footage has nowhere to go. Tried again for good, it was indexed onto the next camera given
                # the same id. The files are left where they are.
                logging.getLogger("nvr.recording").warning("The last segments of a recording were not indexed: its camera %s no longer exists (they stay in %s)",
                                                           camera_id, output_dir)
                self._indexed_lines.pop(list_path, None)
                self._newest_segment.pop(output_dir, None)
                return
            self._database_unavailable(camera_id, exc)
            self._unindexed.append((camera_id, list_path, output_dir))
            return
        self._indexed_lines.pop(list_path, None)  # Each run writes a new list
        self._newest_segment.pop(output_dir, None)
        remove_run_folder_if_empty(output_dir)

    def _index_leftovers(self) -> None:
        """Try again the lists that could not be indexed when their run ended. Blocking: the five-second pass runs it in a thread."""
        waiting, self._unindexed = self._unindexed, []
        for camera_id, list_path, output_dir in waiting:
            self._index_rest(camera_id, list_path, output_dir)

    def _watch_pass(self, camera_id: str, list_path: Path, output_dir: Path, segment_duration: int) -> None:
        """One pass of the watch over a running recording. Blocking, so the watch runs it in a thread.

        It costs the same for a camera connected for a month as for a minute (#481): the list is read from where the last pass stopped,
        the file being written is looked at by name and not found again among every file of the run, the newest indexed segment is one
        index entry, and the camera's row is written when something changed or a few seconds have passed, not on every pass."""
        self._index_csv(camera_id, list_path, output_dir)
        if self._being_written(output_dir):
            self._mark_video_fresh(camera_id, at_most_every=CAMERA_WRITE_SECONDS)
        recent_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=segment_duration * 2)).isoformat()
        with connect() as db:
            latest = _newest_end(db, camera_id)
            if latest and latest < recent_cutoff and db.execute("SELECT recording_fault_since FROM cameras WHERE id=?", (camera_id,)).fetchone()[0] is None:
                # Written here, while the connection is open: leaving the block closes it (#278). Once: the start of the fault is kept.
                db.execute("UPDATE cameras SET recording_fault_since=COALESCE(recording_fault_since,strftime('%Y-%m-%dT%H:%M:%fZ','now')) WHERE id=?", (camera_id,))
        if latest and latest >= recent_cutoff:
            self._set_status(camera_id, "recording", unless_written_within=STATUS_REFRESH_SECONDS)

    def _being_written(self, output_dir: Path) -> bool:
        """Whether FFmpeg has written to this run's newest segment in the last ten seconds.

        The file found last time is looked at alone. Only when it has gone quiet (FFmpeg has moved on to the next segment, or the stream
        has stalled) is the folder listed for a newer name, and then at most every few seconds: listing and reading the time of every
        file of the run on every pass took 141 ms for a run of 43,200 files."""
        now = time.time()
        known = self._newest_segment.get(output_dir)
        if known is not None:
            try:
                if now - os.stat(known[0]).st_mtime <= FRESH_SECONDS:
                    return True
            except OSError:
                known = None  # Removed (retention): look again
            if known is not None and time.monotonic() - known[1] < RELIST_SECONDS:
                return False
        newest = ""
        try:
            with os.scandir(output_dir) as entries:
                for entry in entries:  # Names only: a segment's name is its start time, so the greatest name is the newest
                    if entry.name.startswith("segment-") and entry.name.endswith(".mp4") and entry.name > newest:
                        newest = entry.name
        except OSError:
            return False
        if not newest:
            return False
        path = os.path.join(output_dir, newest)
        self._newest_segment[output_dir] = (path, time.monotonic())
        try:
            return now - os.stat(path).st_mtime <= FRESH_SECONDS
        except OSError:
            return False

    async def _run_continuous(self, camera_id: str, expected_mode: str = "continuous") -> None:
        executable = shutil.which("ffmpeg")
        if not executable:
            await self._status(camera_id, "unavailable", "FFmpeg is not installed")
            return
        segment_duration = segment_seconds()
        failures = 0
        while not self.stopping:
            row = self._camera(camera_id)
            if row is None or not row["enabled"] or not row["recording_enabled"] or self._desired_mode(camera_id) != expected_mode:
                await self._status(camera_id, "stopped")
                return
            source = await asyncio.to_thread(self._continuous_source, camera_id, row)  # It writes the camera's status when the source cannot be used
            if source is None:
                return
            try:
                from .storage import camera_recordings_dir
                output_dir = camera_recordings_dir(camera_id) / uuid.uuid4().hex
                self._active_runs.add(output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                await self._status(camera_id, "unavailable", "Assigned recording volume is missing or not writable", fault="disk_unavailable")
                await asyncio.sleep(5)
                continue
            list_path = output_dir / "segments.csv"
            await self._status(camera_id, "connecting")
            args = _continuous_args(executable, bool(row["audio_enabled"]), output_dir, list_path, segment_duration)
            env = os.environ.copy()
            env["TZ"] = "UTC"
            process: asyncio.subprocess.Process | None = None
            progress_task: asyncio.Task[None] | None = None
            indexed = False
            try:
                process = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=env
                )
                progress_task = asyncio.create_task(camera_health.watch_progress(process.stdout, camera_id), name=f"progress-{camera_id}")
                if process.stdin is None:
                    raise OSError("Unable to pass stream source to FFmpeg")
                process.stdin.write(_manifest_bytes(source))
                await process.stdin.drain()
                process.stdin.close()
                began = time.monotonic()
                await self._watch_continuous_output(camera_id, process, list_path, output_dir, segment_duration)
                await self._end_ffmpeg(process)
                await asyncio.to_thread(self._index_rest, camera_id, list_path, output_dir)
                indexed = True
                if self.stopping:
                    return
                failures = failures_after(failures, time.monotonic() - began)
                await self._status(camera_id, "reconnecting", "Camera stream interrupted; retrying", fault=await _stream_fault(source))
            except asyncio.CancelledError:
                await self._end_ffmpeg(process)
                raise
            except OSError:
                failures += 1
                await self._status(camera_id, "reconnecting", "Camera stream could not start; retrying", fault=await _stream_fault(source))
            except Exception:  # noqa: BLE001 - a database error (or anything else) must not end a camera's recording for good (#335)
                failures += 1
                logging.getLogger("nvr.recording").exception("Recording for camera %s was interrupted; retrying", camera_id)
                try:
                    await self._status(camera_id, "reconnecting", "Recording was interrupted; retrying")
                except sqlite3.Error:
                    pass
            finally:
                # However the watch ended, the FFmpeg this pass started is ended before another is started beside it (#335), and what it
                # finished writing is indexed (#483). Not in a thread: this also runs as the recording is cancelled, and must not be cut short.
                await self._end_ffmpeg(process)
                if not indexed:
                    self._index_rest(camera_id, list_path, output_dir)
                self._active_runs.discard(output_dir)
                if progress_task:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
            await asyncio.sleep(reconnect_delay(failures))
