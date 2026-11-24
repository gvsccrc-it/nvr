"""Two-way audio (#147): talking through a camera's speaker over the ONVIF audio backchannel.

The browser sends the microphone as 8 kHz mono 16-bit PCM over a WebSocket (``app/api/talk.py``). This module turns that into G.711 and delivers it to the camera:
an RTSP session opened with the ONVIF backchannel requirement (``Require: www.onvif.org/ver20/backchannel``), in which the camera describes an audio track it
receives (``a=sendonly`` in its SDP), set up over the RTSP connection itself (interleaved RTP) and played; the audio then goes out as RTP packets on that
connection. Only one person talks through a camera at a time (``Talkers``).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import itertools
import json
import re
import secrets
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urljoin, urlsplit

import numpy as np

from app import camera_destination
from app.audit import record
from app.store import connect

BACKCHANNEL_REQUIRE = "www.onvif.org/ver20/backchannel"
SAMPLE_RATE = 8000
FRAME_SAMPLES = 160  # 20 ms
MAX_INPUT_BYTES = 16000  # One message from the browser: a second of audio is far more than it sends
MAX_TALK_SECONDS = 300
# The most a camera's RTSP answer may carry (#469). A session description is a few kilobytes; a camera, or anything answering at its address,
# that declares more is refused before its body is read, and the talk ends as a camera failure. Without it, ``readexactly`` held whatever length was
# declared (gigabytes, streamed for the whole talk) in the web process's memory.
MAX_ANSWER_BYTES = 64 * 1024
MAX_KEPT_BYTES = MAX_TALK_SECONDS * SAMPLE_RATE * 2  # The longest talk, as 16-bit samples: all that is ever kept of one
IDLE_SECONDS = 10
RECORDING_KEEP_DAYS = 30
ROLES = ("administrator", "operator")
CONNECT_TIMEOUT = 5.0
USER_AGENT = "NVR-talk/1.0"


class BackchannelError(Exception):
    """The camera could not be talked through."""


class BackchannelUnsupported(BackchannelError):
    """The camera has no speaker we can send to (no backchannel, or a codec other than G.711)."""


def answer_length(value: str | None) -> int:
    """The body length an RTSP answer declares, refused (``ValueError``) when it is not a plain number of at most ``MAX_ANSWER_BYTES`` (#469)."""
    text = (value or "").strip() or "0"
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"The camera declared an answer length that is not a number: {text[:20]!r}")
    length = int(text)
    if length > MAX_ANSWER_BYTES:
        raise ValueError(f"The camera declared an answer of {length} bytes; at most {MAX_ANSWER_BYTES} are accepted")
    return length


class TalkBusy(Exception):
    def __init__(self, holder: str) -> None:
        super().__init__(holder)
        self.holder = holder


# --- G.711 -------------------------------------------------------------------------------------------------------------------------------

def encode_ulaw(pcm: np.ndarray) -> bytes:
    """G.711 mu-law from 16-bit samples."""
    samples = pcm.astype(np.int32)
    sign = np.where(samples < 0, 0x80, 0)
    magnitude = np.minimum(np.abs(samples), 32635) + 0x84
    exponent = np.floor(np.log2(magnitude)).astype(np.int32) - 7
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return bytes((~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8))


def encode_alaw(pcm: np.ndarray) -> bytes:
    """G.711 A-law from 16-bit samples."""
    samples = pcm.astype(np.int32)
    sign = np.where(samples >= 0, 0x80, 0)
    magnitude = np.minimum(np.where(samples < 0, -samples - 1, samples), 32767)
    exponent = np.where(magnitude >= 256, np.floor(np.log2(np.maximum(magnitude, 1))).astype(np.int32) - 7, 0)
    mantissa = np.where(magnitude >= 256, (magnitude >> (np.maximum(exponent, 1) + 3)) & 0x0F, magnitude >> 4)
    return bytes(((sign | (exponent << 4) | mantissa) ^ 0x55).astype(np.uint8))


ENCODERS = {"PCMU": encode_ulaw, "PCMA": encode_alaw}
PAYLOAD_TYPES = {"PCMU": 0, "PCMA": 8}
STATIC_NAMES = {payload: name for name, payload in PAYLOAD_TYPES.items()}


# --- RTSP backchannel ------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AudioTrack:
    codec: str
    payload_type: int
    control: str


def parse_sdp(text: str) -> list[AudioTrack]:
    """The audio tracks the camera receives (marked ``a=sendonly`` from its side) that carry G.711, in the order the camera lists them."""
    tracks: list[AudioTrack] = []
    media: dict[str, object] | None = None

    def close() -> None:
        if not media or media["kind"] != "audio" or media["direction"] != "sendonly":
            return
        names = dict(media["names"])  # payload type -> codec name from a=rtpmap
        for payload in media["formats"]:
            name = names.get(payload) or STATIC_NAMES.get(payload)
            if name in ENCODERS and (PAYLOAD_TYPES[name] == payload or payload >= 96):  # A static type, or a dynamic one the camera named
                tracks.append(AudioTrack(name, payload, str(media["control"] or "")))
                return

    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if line.startswith("m="):
            close()
            parts = line[2:].split()
            media = {"kind": parts[0] if parts else "", "formats": [int(item) for item in parts[3:] if item.isdigit()], "direction": "", "names": [], "control": ""}
        elif media is not None and line.startswith("a="):
            attribute = line[2:]
            if attribute in {"sendonly", "recvonly", "sendrecv", "inactive"}:
                media["direction"] = attribute
            elif attribute.startswith("rtpmap:"):
                match = re.match(r"rtpmap:(\d+)\s+([A-Za-z0-9._-]+)/(\d+)", attribute)
                if match and int(match.group(3)) == SAMPLE_RATE:
                    media["names"].append((int(match.group(1)), match.group(2).upper()))
            elif attribute.startswith("control:"):
                media["control"] = attribute[len("control:"):].strip()
    close()
    return tracks


def _digest(challenge: str, username: str, password: str, method: str, uri: str) -> str:
    fields = dict(re.findall(r'(\w+)="?([^",]+)"?', challenge))
    realm, nonce, qop = fields.get("realm", ""), fields.get("nonce", ""), fields.get("qop", "")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()  # noqa: S324 - RTSP digest is MD5 by definition
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()  # noqa: S324
    if "auth" in qop.split(","):
        cnonce, count = secrets.token_hex(8), "00000001"
        response = hashlib.md5(f"{ha1}:{nonce}:{count}:{cnonce}:auth:{ha2}".encode()).hexdigest()  # noqa: S324
        extra = f', qop=auth, nc={count}, cnonce="{cnonce}"'
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()  # noqa: S324
        extra = ""
    opaque = f', opaque="{fields["opaque"]}"' if "opaque" in fields else ""
    return f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}"{extra}{opaque}'


UNPROTECTED_REFUSED = ("The camera asked for its password to be sent unprotected (Basic sign-in over plain RTSP), so it was not sent. If this "
                      "camera can do nothing else, an administrator can allow it for this camera under Cameras, Talk.")


class RtspBackchannel:
    """One RTSP session in which this process sends audio to a camera. Use ``connect`` then ``send`` then ``close``."""

    def __init__(self, url: str, *, allow_basic: bool = False, allow_public: bool = False) -> None:
        parts = urlsplit(url)
        self.allow_public = allow_public  # The camera may be at a public address (#474)
        # Whether a request for Basic sign-in may be answered. Talking runs over plain RTSP only, where Basic is the user name and
        # password merely encoded: answered, anything at the camera's address that asks for it is handed them (#476). Off unless an
        # administrator allowed it for this camera.
        self.allow_basic = allow_basic
        if parts.scheme != "rtsp" or not parts.hostname:
            raise BackchannelUnsupported("Talking needs a plain rtsp:// camera address")
        self.host, self.port = parts.hostname, parts.port or 554
        self.username, self.password = unquote(parts.username or ""), unquote(parts.password or "")
        netloc = f"[{self.host}]" if ":" in self.host else self.host
        self.url = f"rtsp://{netloc}:{self.port}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")
        self.track: AudioTrack | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._cseq = itertools.count(1)
        self._session = ""
        self._challenge = ""
        self._channel = 0
        self._sequence = secrets.randbelow(65536)
        self._timestamp = secrets.randbelow(2**32)
        self._ssrc = secrets.randbelow(2**32)
        self._drain: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._responses: dict[int, asyncio.Future] = {}
        self.closed = asyncio.Event()
        self.packets = 0
        self.failure = "The camera closed the connection"  # Why the connection ended, for whoever was waiting for an answer

    # -- the wire
    async def _read_frames(self) -> None:
        """Read everything the camera sends: RTSP responses go to whoever is waiting for them, interleaved media (the camera has none for us) is dropped."""
        reader = self._reader
        assert reader is not None
        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":
                    header = await reader.readexactly(3)
                    await reader.readexactly(struct.unpack(">H", header[1:])[0])
                    continue
                head = first + await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                status = int(lines[0].split()[1]) if len(lines[0].split()) > 1 else 0
                headers: dict[str, str] = {}
                for key, _, value in (line.partition(":") for line in lines[1:] if ":" in line):
                    name = key.strip().lower()
                    # A camera may offer several ways to sign in, one header each: keep them all, so Digest is chosen when it is offered
                    headers[name] = f"{headers[name]}\n{value.strip()}" if name == "www-authenticate" and name in headers else value.strip()
                body = await reader.readexactly(answer_length(headers.get("content-length")))
                future = self._responses.pop(int(headers.get("cseq", "0") or 0), None)
                if future is not None and not future.done():
                    future.set_result((status, headers, body))
        except ValueError as exc:
            self.failure = str(exc) if str(exc).startswith("The camera declared") else self.failure
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError, IndexError):
            pass
        finally:
            self.closed.set()
            if self._writer is not None:
                self._writer.close()  # Nothing more is read, so nothing more is let in: the camera cannot keep filling a buffer here
            for future in self._responses.values():
                if not future.done():
                    future.set_exception(BackchannelError(self.failure))

    async def _request(self, method: str, url: str, headers: dict[str, str] | None = None, *, wait: bool = True, authorise: bool = True) -> tuple[int, dict[str, str], bytes]:
        writer = self._writer
        if writer is None or self.closed.is_set():
            raise BackchannelError("The camera closed the connection")
        cseq = next(self._cseq)
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}", f"User-Agent: {USER_AGENT}", f"Require: {BACKCHANNEL_REQUIRE}"]
        if self._session:
            lines.append(f"Session: {self._session}")
        if authorise and self._challenge and self.username:
            if self._challenge.lower().startswith("digest"):
                lines.append(f"Authorization: {_digest(self._challenge, self.username, self.password, method, url)}")
            elif self.allow_basic:  # Chosen only where an administrator allowed it for this camera (_choose)
                lines.append("Authorization: Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode())
        lines += [f"{key}: {value}" for key, value in (headers or {}).items()]
        future: asyncio.Future | None = None
        if wait:
            future = asyncio.get_running_loop().create_future()
            self._responses[cseq] = future
        async with self._lock:
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
            await asyncio.wait_for(writer.drain(), CONNECT_TIMEOUT)
        if future is None:
            return 0, {}, b""
        try:
            return await asyncio.wait_for(future, CONNECT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            self._responses.pop(cseq, None)
            raise BackchannelError(f"The camera did not answer {method}") from exc

    async def _authorised(self, method: str, url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        status, response, body = await self._request(method, url, headers)
        if status == 401 and response.get("www-authenticate") and self.username:
            self._challenge = self._choose(response["www-authenticate"])
            status, response, body = await self._request(method, url, headers)
        if status == 401:
            raise BackchannelError("The camera refused the sign-in")
        return status, response, body

    def _choose(self, offered: str) -> str:
        """The sign-in to answer, of those the camera offered: Digest when offered, Basic only where this camera is allowed it."""
        ways = [way.strip() for way in offered.split("\n") if way.strip()]
        digest = next((way for way in ways if way.lower().startswith("digest")), None)
        if digest:
            return digest
        if self.allow_basic:
            return ways[0]
        raise BackchannelError(UNPROTECTED_REFUSED)

    # -- the session
    async def connect(self) -> AudioTrack:
        try:
            # The camera address rule, on the address actually connected to: the name is looked up once and that address checked (#474)
            address = await asyncio.wait_for(camera_destination.aresolve(self.host, self.port, allow_public=self.allow_public), CONNECT_TIMEOUT)
            self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(address, self.port), CONNECT_TIMEOUT)
        except camera_destination.RefusedDestination as exc:
            raise BackchannelError(str(exc)) from None
        except (OSError, asyncio.TimeoutError) as exc:
            raise BackchannelError("The camera cannot be reached") from exc
        self._drain = asyncio.create_task(self._read_frames())
        try:
            status, headers, body = await self._authorised("DESCRIBE", self.url, {"Accept": "application/sdp"})
            if status in {551, 405, 501} or (status == 400 and not body):
                raise BackchannelUnsupported("This camera does not offer an audio backchannel")
            if status != 200:
                raise BackchannelError(f"The camera answered DESCRIBE with {status}")
            tracks = parse_sdp(body.decode("utf-8", "replace"))
            if not tracks:
                raise BackchannelUnsupported("This camera's speaker does not take G.711 audio, or it has no backchannel")
            self.track = tracks[0]
            base = headers.get("content-base") or self.url
            control = self.track.control
            target = base if not control or control == "*" else control if "://" in control else urljoin(base if base.endswith("/") else base + "/", control)
            status, headers, _ = await self._authorised("SETUP", target, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
            if status != 200:
                raise BackchannelError(f"The camera would not set up its speaker ({status})")
            self._session = headers.get("session", "").split(";")[0].strip()
            timeout = re.search(r"timeout=(\d+)", headers.get("session", ""))
            match = re.search(r"interleaved=(\d+)", headers.get("transport", ""))
            self._channel = int(match.group(1)) if match else 0
            status, _, _ = await self._authorised("PLAY", self.url, {"Range": "npt=0.000-"})
            if status != 200:
                raise BackchannelError(f"The camera would not start its speaker ({status})")
            self._keepalive = asyncio.create_task(self._keep_alive(int(timeout.group(1)) if timeout else 60))
            return self.track
        except BaseException:
            await self.close()
            raise

    async def _keep_alive(self, timeout: int) -> None:
        with contextlib.suppress(Exception):
            while not self.closed.is_set():
                await asyncio.sleep(max(5, min(timeout // 2, 25)))
                await self._request("OPTIONS", self.url, wait=False)

    async def send(self, payload: bytes) -> None:
        """Send one RTP packet's worth of encoded audio."""
        writer, track = self._writer, self.track
        if writer is None or track is None or self.closed.is_set():
            raise BackchannelError("The camera closed the connection")
        header = struct.pack(">BBHII", 0x80, track.payload_type, self._sequence, self._timestamp, self._ssrc)
        self._sequence = (self._sequence + 1) & 0xFFFF
        self._timestamp = (self._timestamp + len(payload)) & 0xFFFFFFFF
        packet = header + payload
        async with self._lock:
            writer.write(b"$" + bytes([self._channel]) + struct.pack(">H", len(packet)) + packet)
            await asyncio.wait_for(writer.drain(), CONNECT_TIMEOUT)
        self.packets += 1

    async def close(self) -> None:
        if self._keepalive is not None:
            self._keepalive.cancel()
        if self._writer is not None and not self.closed.is_set() and self._session:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._request("TEARDOWN", self.url), 1.0)
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer.wait_closed(), 1.0)
        if self._drain is not None:
            self._drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._drain
        self.closed.set()


# --- one talker at a time -----------------------------------------------------------------------------------------------------------

@dataclass
class Talk:
    id: str
    camera_id: str
    user_id: int
    username: str
    started_at: str
    started: float = field(default_factory=time.monotonic)


class Talkers:
    """Who is talking through which camera, in this process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, Talk] = {}

    def begin(self, camera_id: str, user_id: int, username: str) -> Talk:
        with self._lock:
            held = self._active.get(camera_id)
            if held is not None:
                raise TalkBusy(held.username)
            talk = Talk(secrets.token_hex(16), camera_id, user_id, username, datetime.now(timezone.utc).isoformat())
            self._active[camera_id] = talk
            return talk

    def end(self, talk: Talk) -> None:
        with self._lock:
            if self._active.get(talk.camera_id) is talk:
                del self._active[talk.camera_id]

    def holder(self, camera_id: str) -> dict[str, str] | None:
        with self._lock:
            held = self._active.get(camera_id)
        return {"username": held.username, "since": held.started_at} if held else None

    def holders(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {camera_id: {"username": held.username, "since": held.started_at} for camera_id, held in self._active.items()}


talkers = Talkers()


# --- settings and the record --------------------------------------------------------------------------------------------------------

def settings(db, camera_id: str) -> dict[str, object]:
    row = db.execute("SELECT enabled,roles_json,record_audio,allow_basic FROM camera_talk WHERE camera_id=?", (camera_id,)).fetchone()
    if row is None:
        return {"enabled": False, "roles": ["administrator"], "record_audio": False, "allow_basic": False}
    try:
        roles = [role for role in json.loads(row["roles_json"]) if role in ROLES]
    except (TypeError, ValueError):
        roles = []
    return {"enabled": bool(row["enabled"]), "roles": roles, "record_audio": bool(row["record_audio"]), "allow_basic": bool(row["allow_basic"])}


def allowed(config: dict[str, object], role: str) -> bool:
    return bool(config["enabled"]) and role in config["roles"]  # type: ignore[operator]


def wav_bytes(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)
    return out.getvalue()


def begin_record(talk: Talk) -> None:
    with connect() as db:
        db.execute("INSERT INTO talk_sessions(id,camera_id,user_id,username,started_at) VALUES(?,?,?,?,?)",
                   (talk.id, talk.camera_id, talk.user_id, talk.username, talk.started_at))
        record(db, talk.user_id, "talk_started", "camera", talk.camera_id, session=talk.id)


def end_record(talk: Talk, reason: str, pcm: bytes | None, codec: str | None = None) -> None:
    seconds = round(time.monotonic() - talk.started, 2)
    with connect() as db:
        db.execute("UPDATE talk_sessions SET ended_at=?,seconds=?,ended_reason=?,codec=?,audio_wav=? WHERE id=?",
                   (datetime.now(timezone.utc).isoformat(), seconds, reason, codec, wav_bytes(pcm) if pcm else None, talk.id))
        record(db, talk.user_id, "talk_ended", "camera", talk.camera_id, session=talk.id, seconds=seconds, reason=reason, codec=codec, recorded=bool(pcm))
        db.execute("UPDATE talk_sessions SET audio_wav=NULL WHERE audio_wav IS NOT NULL AND started_at < ?",
                   ((datetime.now(timezone.utc) - timedelta(days=RECORDING_KEEP_DAYS)).isoformat(),))  # Kept audio goes after RECORDING_KEEP_DAYS; the row stays


def clear_old_recordings(now: datetime | None = None) -> int:
    """Clear kept talk audio older than RECORDING_KEEP_DAYS, whether or not anyone talks again (#478). It was only cleared when a later
    talk ended, so the last recordings stayed past the time promised once talking stopped being used. The session's row (who, when, how
    long) stays. Reads for due rows first: with nothing due, nothing is written and no write lock is taken (#280)."""
    from .store import has_row

    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=RECORDING_KEEP_DAYS)).isoformat()
    with connect() as db:
        if not has_row(db, "SELECT 1 FROM talk_sessions WHERE audio_wav IS NOT NULL AND started_at<? LIMIT 1", (cutoff,)):
            return 0
        return db.execute("UPDATE talk_sessions SET audio_wav=NULL WHERE audio_wav IS NOT NULL AND started_at<?", (cutoff,)).rowcount


class Talking:
    """One talk in progress: turns what the browser sends into G.711 packets for the camera, and keeps the audio when asked to."""

    def __init__(self, backchannel: RtspBackchannel, keep_audio: bool) -> None:
        assert backchannel.track is not None
        self.backchannel = backchannel
        self.encode = ENCODERS[backchannel.track.codec]
        self.keep = bytearray() if keep_audio else None
        self._pending = np.zeros(0, dtype=np.int16)

    async def feed(self, data: bytes) -> None:
        if not data or len(data) > MAX_INPUT_BYTES or len(data) % 2:
            raise ValueError("Audio must be 16-bit samples, in pieces of up to a second")
        if self.keep is not None and len(self.keep) < MAX_KEPT_BYTES:
            # What is kept is bounded by size as well as by time: a client can send faster than it speaks, and everything it sent in
            # five minutes used to be held in memory and written out (#386). The longest talk at its real rate fits exactly.
            self.keep += data[:MAX_KEPT_BYTES - len(self.keep)]
        self._pending = np.concatenate([self._pending, np.frombuffer(data, dtype="<i2")])
        while len(self._pending) >= FRAME_SAMPLES:
            frame, self._pending = self._pending[:FRAME_SAMPLES], self._pending[FRAME_SAMPLES:]
            await self.backchannel.send(self.encode(frame))
