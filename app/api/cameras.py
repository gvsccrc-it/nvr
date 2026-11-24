"""Camera inventory, stream sources, credentials, audio probing and motion sources."""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app import console
from app.principal import Administrator, CurrentUser, is_browser_session
from app.store import connect, read_one
from app.credentials import encrypt, decrypt
from app.recording import masked_stream_url, probe_stream, safe_stream_url
from app.health import recording_health
from app.onvif_client import normalise_host
from app.onvif_stream import list_camera_stream_profiles, placeholder_stream_url, resolve_camera_stream
from app import camera_destination, capabilities as camera_capabilities, diagnostics, privacy
from app.retention import effective_retention
from app.api.policies import effective_schedule_view
from app.audit import record as audit_record
from app.privacy_check import request_check as request_mask_check

router = APIRouter()


CameraBrand = Literal["auto", "acti", "axis", "bosch", "dahua", "hanwha", "hikvision", "reolink", "samsung", "uniview", "other"]


class CameraSourceRequest(BaseModel):
    stream_url: str | None = Field(default=None, min_length=8, max_length=2048)
    camera_ip: str | None = Field(default=None, min_length=1, max_length=253)
    camera_username: str | None = Field(default=None, max_length=256)
    camera_password: str | None = Field(default=None, max_length=1024)
    camera_brand: CameraBrand = "auto"
    onvif_profile_token: str | None = Field(default=None, max_length=256)
    live_substream_profile_token: str | None = Field(default=None, max_length=256)
    device_service_url: str | None = Field(default=None, max_length=2048)
    public_address: bool = False  # "This camera is at a public address" (#474): honoured only from an administrator signed in with a browser

    @model_validator(mode="after")
    def validate_source(self) -> "CameraSourceRequest":
        if bool(self.stream_url) == bool(self.camera_ip):
            raise ValueError("Provide either a camera IP address or an RTSP stream URL")
        if self.live_substream_profile_token and (not self.camera_ip or not self.onvif_profile_token):
            raise ValueError("A live substream requires an ONVIF camera address and a selected main profile")
        if self.live_substream_profile_token and self.live_substream_profile_token == self.onvif_profile_token:
            raise ValueError("Choose a different profile for the optional live substream")
        return self

    @field_validator("device_service_url")
    @classmethod
    def validate_device_service_url(cls, value: str | None) -> str | None:
        return _validate_onvif_service_url(value)

    @field_validator("camera_ip")
    @classmethod
    def normalise_camera_ip(cls, value: str | None) -> str | None:
        """Stored and compared as one spelling: an IP address or host name in lower case, without brackets or spaces."""
        return None if value is None else normalise_host(value)


class CameraProbeRequest(CameraSourceRequest):
    @model_validator(mode="after")
    def validate_credentials(self) -> "CameraProbeRequest":
        if bool(self.camera_username) != bool(self.camera_password):
            raise ValueError("Provide both camera username and password, or leave both blank")
        return self


class CameraSourceUpdateRequest(CameraSourceRequest):
    clear_credentials: bool = False

    @model_validator(mode="after")
    def validate_credential_choice(self) -> "CameraSourceUpdateRequest":
        if self.clear_credentials and (self.camera_username or self.camera_password):
            raise ValueError("Clear saved credentials or provide new credentials, not both")
        if self.clear_credentials and self.stream_url:
            parsed = urlsplit(self.stream_url)
            if parsed.username or parsed.password:
                raise ValueError("Remove credentials from the direct stream URL before clearing saved credentials")
        return self


class CameraProfileRequest(BaseModel):
    camera_ip: str = Field(min_length=1, max_length=253)

    @field_validator("camera_ip")
    @classmethod
    def normalise_camera_ip(cls, value: str) -> str:
        return normalise_host(value)

    camera_username: str | None = Field(default=None, max_length=256)
    camera_password: str | None = Field(default=None, max_length=1024)
    camera_brand: CameraBrand = "auto"
    device_service_url: str | None = Field(default=None, max_length=2048)
    public_address: bool = False

    @model_validator(mode="after")
    def validate_credentials(self) -> "CameraProfileRequest":
        if bool(self.camera_username) != bool(self.camera_password):
            raise ValueError("Provide both camera username and password, or leave both blank")
        return self


def _validate_onvif_service_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ONVIF motion service must be an HTTP(S) device-service URL without credentials, query, or fragment")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid ONVIF service port")
    return value.strip()


PUBLIC_TICK_FROM_TOKEN = "Only an administrator signed in with a browser can allow a camera at a public address, not an API token"


def public_permission(payload: Any, user: Any) -> bool:
    """Whether this request may reach a camera at a public address: its tick, "This camera is at a public address", from an administrator
    signed in with a browser (#474). The tick from an API token is refused, so a leaked token cannot point the NVR at the internet. Bulk Add
    and CSV import never tick it."""
    if not getattr(payload, "public_address", False):
        return False
    if not is_browser_session(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PUBLIC_TICK_FROM_TOKEN)
    return True


def _destination_hosts(payload: Any) -> list[str]:
    """Every host a camera request names: its address, its direct stream address and its ONVIF device and event addresses."""
    hosts: list[str] = []
    for name in ("camera_ip", "stream_url", "device_service_url", "motion_service_url"):
        value = (getattr(payload, name, None) or "").strip()
        host = (urlsplit(value).hostname if "://" in value else value) if value else None
        if host:
            hosts.append(host)
    return list(dict.fromkeys(hosts))


async def check_destinations(payload: Any, *, allow_public: bool) -> bool:
    """Refuse (422) a camera address the destination rule refuses, before anything connects (#474). True when one of the addresses is public
    (and so allowed). A name that cannot be looked up now is left to the connection, which looks it up and is checked again there."""
    def judge() -> bool:
        public = False
        for host in _destination_hosts(payload):
            try:
                addresses = camera_destination.addresses_of(host)
            except (OSError, UnicodeError):
                continue
            for address in addresses:
                public = camera_destination.check(address, allow_public=allow_public) == "public" or public
        return public
    try:
        return await asyncio.to_thread(judge)
    except camera_destination.RefusedDestination as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class MotionZoneRequest(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def fit_frame(self) -> "MotionZoneRequest":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Motion zone must fit inside the normalized frame")
        return self


class CameraCreateRequest(CameraProbeRequest):
    name: str = Field(min_length=1, max_length=120)
    recording_mode: Literal["motion", "continuous", "disabled"] = "motion"
    recording_enabled: bool = False
    motion_service_url: str | None = Field(default=None, max_length=2048)
    motion_username: str | None = Field(default=None, max_length=256)
    motion_password: str | None = Field(default=None, max_length=1024)
    motion_pre_roll_seconds: int = Field(default=10, ge=0, le=120)
    motion_post_roll_seconds: int = Field(default=30, ge=1, le=600)
    motion_fallback_enabled: bool = False
    motion_zones: list[MotionZoneRequest] = Field(default_factory=list, max_length=16)
    motion_sensitivity: int = Field(default=50, ge=1, le=100)
    onvif_uuid: str | None = Field(default=None, min_length=36, max_length=36)
    save_anyway: bool = False  # Save the camera although its video could not be opened; recording stays off and the video is rechecked in the background

    @field_validator("onvif_uuid")
    @classmethod
    def validate_onvif_uuid(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("ONVIF identity must be a UUID") from exc

    @field_validator("motion_service_url")
    @classmethod
    def validate_motion_service_url(cls, value: str | None) -> str | None:
        return _validate_onvif_service_url(value)

    @field_validator("motion_username", "motion_password")
    @classmethod
    def trim_motion_credentials(cls, value: str | None) -> str | None:
        return value if value else None


class CameraCredentialRotateRequest(CameraProbeRequest):
    pass


class MotionSourceUpdateRequest(BaseModel):
    service_url: str | None = Field(default=None, max_length=2048)
    username: str | None = Field(default=None, max_length=256)
    password: str | None = Field(default=None, max_length=1024)
    pre_roll_seconds: int | None = Field(default=None, ge=0, le=120)
    post_roll_seconds: int | None = Field(default=None, ge=1, le=600)
    fallback_enabled: bool | None = None
    zones: list[MotionZoneRequest] | None = Field(default=None, max_length=16)
    sensitivity: int | None = Field(default=None, ge=1, le=100)

    @field_validator("service_url")
    @classmethod
    def validate_service_url(cls, value: str | None) -> str | None:
        validated = _validate_onvif_service_url(value)
        return validated


class CameraUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    recording_mode: Literal["motion", "continuous", "disabled"] | None = None
    recording_enabled: bool | None = None
    motion_pre_roll_seconds: int | None = Field(default=None, ge=0, le=120)
    motion_post_roll_seconds: int | None = Field(default=None, ge=1, le=600)
    audio_enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str | None) -> str | None:
        value = value.strip() if value is not None else None
        if value == "":
            raise ValueError("Camera name is required")
        return value


@router.post("/api/cameras/onvif-profiles")
async def camera_onvif_profiles(
    payload: CameraProfileRequest,
    user: Administrator,
) -> dict[str, object]:
    allowed = public_permission(payload, user)
    await check_destinations(payload, allow_public=allowed)
    try:
        with camera_destination.public_permitted(allowed):
            service_url, profiles = await list_camera_stream_profiles(
                payload.camera_ip, payload.camera_username or "", payload.camera_password or "",
                payload.device_service_url, payload.camera_brand,
            )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"device_service_url": service_url, "profiles": [profile.__dict__ for profile in profiles]}


# Where a camera is on the network: its address, ONVIF device address, profile tokens, and the scheme, host and port of its stream. Only
# administrators are given these (#377): everyone else (operators, read-scope API tokens, video walls) gets them empty.
ADDRESS_FIELDS = ("camera_ip", "device_service_url", "onvif_profile_token", "live_substream_profile_token", "stream_url", "public_address")


def sees_camera_addresses(user: Any) -> bool:
    """An administrator in their own session, or an API token given the administration scope. Every token belongs to an administrator, so
    the owner's role alone would hand a read-only token or a video wall the whole map."""
    return user.is_administrator and not user.wall and (user.scopes is None or "admin" in user.scopes)


def camera_view(row: sqlite3.Row) -> dict[str, object]:
    try:
        visible_url = masked_stream_url(decrypt(row["stream_url_ciphertext"]))
    except RuntimeError:
        visible_url = "rtsp://credentials-unavailable"
    return {
        "id": row["id"],
        "name": row["name"],
        "group_name": row["group_name"] if "group_name" in row.keys() else None,
        "storage_volume_id": row["storage_volume_id"] if "storage_volume_id" in row.keys() else None,
        "camera_ip": row["camera_ip"] if "camera_ip" in row.keys() else None,
        "camera_brand": row["camera_brand"] if "camera_brand" in row.keys() else "auto",
        "device_service_url": row["device_service_url"] if "device_service_url" in row.keys() else None,
        "onvif_profile_token": row["onvif_profile_token"] if "onvif_profile_token" in row.keys() else None,
        "live_substream_profile_token": row["live_substream_profile_token"] if "live_substream_profile_token" in row.keys() else None,
        # Whether the camera has an ONVIF device service (so may have PTZ and the like), without saying where it is: everyone gets this (#377).
        "has_onvif_device": bool(row["device_service_url"]) if "device_service_url" in row.keys() else False,
        # Whether the camera may be at a public address (#474): "allowed" (ticked), "grandfathered" (from before the rule) or None. Administrators only.
        "public_address": row["public_address"] if "public_address" in row.keys() else None,
        "archived_at": row["archived_at"] if "archived_at" in row.keys() else None,
        "stream_url": visible_url,
        "enabled": bool(row["enabled"]),
        "recording_mode": row["recording_mode"],
        "recording_enabled": bool(row["recording_enabled"]),
        "recording_status": row["recording_status"],
        "last_video_at": row["last_video_at"],
        "last_footage_write_at": row["last_footage_write_at"],
        "recording_fault_since": row["recording_fault_since"],
        "recording_health": recording_health(
            enabled=bool(row["enabled"]), recording_enabled=bool(row["recording_enabled"]),
            mode=row["recording_mode"], effective_mode=row["recording_mode"],
            recording_status=row["recording_status"], motion_source_status=row["motion_source_status"],
            last_video_at=row["last_video_at"], last_footage_write_at=row["last_footage_write_at"],
        ),
        "motion_source_status": row["motion_source_status"],
        "motion_source_configured": bool(row["motion_service_url"]),
        "motion_pre_roll_seconds": row["motion_pre_roll_seconds"],
        "motion_post_roll_seconds": row["motion_post_roll_seconds"],
        "motion_fallback_enabled": bool(row["motion_fallback_enabled"]),
        "motion_zones": json.loads(row["motion_zones_json"]),
        "motion_sensitivity": row["motion_sensitivity"],
        "server_motion_status": row["server_motion_status"],
        "video": {"codec": row["video_codec"], "width": row["video_width"], "height": row["video_height"]},
        "live_substream_available": bool(row["live_substream_url_ciphertext"]),
        "audio_supported": None if row["audio_supported"] is None else bool(row["audio_supported"]),
        "audio_enabled": bool(row["audio_enabled"]),
        "audio_status": "enabled" if row["audio_enabled"] else ("unknown" if row["audio_supported"] is None else ("available_disabled" if bool(row["audio_supported"]) else "unsupported")),
        "last_probe_at": row["last_probe_at"],
        "last_error": row["last_error"],
    }


def insert_camera(
    db: sqlite3.Connection,
    *,
    name: str,
    stream_url: str,
    recording_mode: str,
    capability: dict[str, object],
    onvif_uuid: str | None = None,
    group_id: str | None = None,
    camera_ip: str | None = None,
    camera_brand: str = "auto",
    device_service_url: str | None = None,
    onvif_profile_token: str | None = None,
    live_substream_profile_token: str | None = None,
    recording_enabled: bool = False,
    motion_service_url: str | None = None,
    motion_username: str | None = None,
    motion_password: str | None = None,
    motion_pre_roll_seconds: int = 10,
    motion_post_roll_seconds: int = 30,
    motion_fallback_enabled: bool = False,
    motion_zones: list[MotionZoneRequest] | None = None,
    motion_sensitivity: int = 50,
    live_substream_url: str | None = None,
    public_address: str | None = None,
) -> str:
    """Persist a camera only after its stream has passed the shared probe. ``public_address``: ``allowed`` when an administrator in a browser
    ticked that it is at a public address (#474), otherwise None."""
    video = capability["video"]
    if not isinstance(video, dict):
        raise ValueError("A successful stream probe must include video metadata")
    camera_id = str(uuid.uuid4())
    recording_enabled = bool(recording_enabled and recording_mode != "disabled")
    status = "stopped" if not recording_enabled else ("connecting" if recording_mode == "continuous" or motion_service_url or motion_fallback_enabled else "unsupported")
    error = "Configure an ONVIF event service or enable server motion fallback" if recording_enabled and recording_mode == "motion" and not motion_service_url and not motion_fallback_enabled else None
    credentials = None
    if motion_username or motion_password:
        if not motion_username or not motion_password:
            raise ValueError("Provide both ONVIF motion username and password")
        credentials = encrypt(json.dumps({"username": motion_username, "password": motion_password}, separators=(",", ":")))
    db.execute(
        "INSERT INTO cameras(id,name,onvif_uuid,group_id,camera_ip,camera_brand,device_service_url,onvif_profile_token,live_substream_profile_token,stream_url_ciphertext,live_substream_url_ciphertext,audio_supported,audio_enabled,recording_mode,recording_enabled,recording_status,"
        "motion_service_url,motion_credentials_ciphertext,motion_pre_roll_seconds,motion_post_roll_seconds,motion_source_status," 
        "motion_fallback_enabled,motion_zones_json,motion_sensitivity,server_motion_status,last_error," 
        "video_codec,video_width,video_height,public_address,last_probe_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (camera_id, name, onvif_uuid, group_id, camera_ip, camera_brand, device_service_url, onvif_profile_token, live_substream_profile_token,
         encrypt(stream_url.strip()), encrypt(live_substream_url.strip()) if live_substream_url else None, int(bool(capability.get("audio"))), 0, recording_mode, int(recording_enabled), status,
         motion_service_url, credentials, motion_pre_roll_seconds, motion_post_roll_seconds,
         "untested" if motion_service_url else "unsupported", int(motion_fallback_enabled),
         json.dumps([zone.model_dump() for zone in (motion_zones or [])], separators=(",", ":")), motion_sensitivity,
         "waiting" if motion_fallback_enabled else "disabled", error,
         video.get("codec"), video.get("width"), video.get("height"), public_address),
    )
    return camera_id


def record_discovery_probe(db: sqlite3.Connection, device_uuid: str | None, capability: dict[str, object]) -> None:
    if not device_uuid:
        return
    row = db.execute("SELECT id,capabilities FROM discovery_candidates WHERE device_uuid=?", (device_uuid,)).fetchone()
    if row is None:
        return
    states = json.loads(row["capabilities"])
    states["video_stream"] = "supported"
    if isinstance(capability.get("audio"), bool):
        states["audio"] = "supported" if capability["audio"] else "unsupported"
    db.execute("UPDATE discovery_candidates SET capabilities=? WHERE id=?", (json.dumps(states), row["id"]))


def record_discovery_failure(db: sqlite3.Connection, device_uuid: str) -> None:
    row = db.execute("SELECT id,capabilities FROM discovery_candidates WHERE device_uuid=?", (device_uuid,)).fetchone()
    if row is None:
        return
    states = json.loads(row["capabilities"])
    states["video_stream"] = "unavailable"
    db.execute("UPDATE discovery_candidates SET capabilities=? WHERE id=?", (json.dumps(states), row["id"]))


@router.post("/api/cameras/test")
async def test_camera_stream(
    payload: CameraProbeRequest,
    user: Administrator,
) -> dict[str, object]:
    allowed = public_permission(payload, user)
    await check_destinations(payload, allow_public=allowed)
    with camera_destination.public_permitted(allowed):
        try:
            stream_url, _ = await _resolve_camera_source(payload)
            safe_stream_url(stream_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        started = time.monotonic()
        result = await probe_stream(stream_url)
    await asyncio.to_thread(diagnostics.record_event, 
        "stream_probe", outcome="success" if result.get("reachable") else "unavailable",
        elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)),
    )
    return result


async def _resolve_camera_source(payload: CameraProbeRequest, service_url: str | None = None) -> tuple[str, str | None]:
    """Return an RTSP URI and its ONVIF device-service URL, resolving IP inputs safely."""
    if payload.stream_url:
        stream_url = payload.stream_url.strip()
        if payload.camera_username and payload.camera_password:
            parsed = urlsplit(stream_url)
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parsed.port:
                host = f"{host}:{parsed.port}"
            stream_url = urlunsplit((parsed.scheme, f"{quote(payload.camera_username, safe='')}:{quote(payload.camera_password, safe='')}@{host}", parsed.path, parsed.query, ""))
        return stream_url, service_url
    try:
        source = await resolve_camera_stream(
            payload.camera_ip or "", payload.camera_username or "", payload.camera_password or "",
            service_url or payload.device_service_url, payload.onvif_profile_token, payload.camera_brand,
        )
        return source.stream_url, source.device_service_url
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/cameras")
@console.federated(console.split_plain, console.combine_cameras)
def list_cameras(user: CurrentUser) -> dict[str, object]:
    with connect() as db:
        rows = [row for row in db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id ORDER BY c.name COLLATE NOCASE")
                if user.access.can_see(row["id"])]
        cameras = []
        addresses_shown = sees_camera_addresses(user)
        for row in rows:
            camera = camera_view(row)
            if not addresses_shown:
                camera.update(dict.fromkeys(ADDRESS_FIELDS))  # A map of the camera network is for administrators only (#377)
            if row["group_id"] and not user.access.can_see_group(row["group_id"]):
                camera["group_name"] = None  # Visible through another group; the name of the one that is not is not theirs to see.
            schedule = effective_schedule_view(db, row["id"])
            camera.update(schedule)
            retention_days, retention_source = effective_retention(db, row["id"])
            camera["retention_days"] = retention_days
            camera["retention_policy_source"] = retention_source
            camera["recording_health"] = recording_health(
                enabled=bool(row["enabled"]), recording_enabled=bool(row["recording_enabled"]),
                mode=row["recording_mode"], effective_mode=str(schedule["effective_recording_mode"]),
                recording_status=row["recording_status"], motion_source_status=row["motion_source_status"],
                last_video_at=row["last_video_at"], last_footage_write_at=row["last_footage_write_at"],
            )
            cameras.append(camera)
    return {"cameras": cameras}


async def _initial_health(payload: "CameraCreateRequest", service_url: str | None) -> dict[str, dict[str, str]] | None:
    """What an inspection says about the camera's discovery, login, audio, PTZ and motion events, for the health a new camera starts with. Best effort."""
    from app.camera_inspect import inspect_camera
    try:
        report = await asyncio.wait_for(inspect_camera(
            payload.camera_ip or "", payload.camera_username or "", payload.camera_password or "",
            device_service_url=service_url or payload.device_service_url, brand=payload.camera_brand, verify=False), timeout=30)
    except Exception:  # noqa: BLE001 - health is recorded as unknown and rechecked rather than failing the add
        return None
    return report["capabilities"]


def _record_new_camera_capabilities(db: sqlite3.Connection, camera_id: str, payload: "CameraCreateRequest", capability: dict[str, object],
                                    video_problem: str | None, has_events: bool, health: dict[str, dict[str, str]] | None) -> None:
    """The capability health a camera starts with, from what adding it learned (an inspection where ONVIF was used). Video and motion are rechecked in the background when not ok."""
    record = camera_capabilities.record
    record(db, camera_id, "video", "failed" if video_problem else "ok", video_problem or "Video is flowing")
    if health is not None:
        for name in ("discovery", "authentication", "audio", "ptz"):
            record(db, camera_id, name, health[name]["state"], health[name]["message"])
        motion = health["motion"]
        record(db, camera_id, "motion", motion["state"] if has_events or motion["state"] != "ok" else "unknown", motion["message"])
        return
    if video_problem:
        record(db, camera_id, "audio", "unknown", "Not checked: the video could not be opened")
    else:
        record(db, camera_id, "audio", "available" if capability.get("audio") else "unsupported", "The camera carries audio" if capability.get("audio") else "No audio on the stream")
    record(db, camera_id, "discovery", "unsupported", "Added from a direct stream address")
    record(db, camera_id, "authentication", "unknown", "Not checked")
    record(db, camera_id, "ptz", "unknown", "Not checked")
    record(db, camera_id, "motion", "unknown" if has_events else "unsupported", "Motion events have not been checked yet" if has_events else "No ONVIF event service is set")


async def _resolve_new_camera_source(payload: CameraCreateRequest, onvif_resolved: bool) -> tuple[str, str | None, str | None]:
    """The stream address of a new camera, and the device service it was found through. When ONVIF would not give an address and the camera may be saved anyway, a
    placeholder stands in (recording off) and the reason is returned third; the address is looked for again in the background."""
    try:
        stream_url, resolved_service_url = await _resolve_camera_source(payload, payload.motion_service_url)
        safe_stream_url(stream_url)
        return stream_url, resolved_service_url, None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException as exc:
        placeholder = placeholder_stream_url(payload.camera_ip) if payload.save_anyway and onvif_resolved and exc.status_code == 422 else None
        if placeholder is None:
            raise
        return placeholder, payload.motion_service_url, str(exc.detail)[:300]


async def _probe_new_camera_stream(payload: CameraCreateRequest, stream_url: str, unresolved: str | None) -> tuple[dict[str, Any], str | None]:
    """Open the stream and read what it carries. Returns that and the reason video cannot be used, when the camera is being saved anyway."""
    video_problem: str | None = unresolved
    if unresolved is not None:
        return {"reachable": False, "video": {}}, video_problem
    capability = await probe_stream(stream_url)
    if not capability.get("reachable"):
        reason = str(capability.get("reason", "Camera stream could not be opened"))
        if not payload.save_anyway:
            raise HTTPException(status_code=422, detail=reason)
        return {"reachable": False, "video": {}}, reason[:300]
    return capability, video_problem


async def _check_live_substream(payload: Any, resolved_service_url: str | None, capability: dict[str, Any]) -> str:
    """The lower-resolution stream chosen for grid viewing (``payload.live_substream_profile_token``), checked: it resolves, opens, and is
    smaller than the main stream (``capability``). Returns its address, or raises a 422 saying which check failed. One check for adding a
    camera and for changing its source (#387); a network error while it is looked up is "could not be resolved" for both."""
    try:
        substream = await resolve_camera_stream(
            payload.camera_ip or "", payload.camera_username or "", payload.camera_password or "",
            resolved_service_url or payload.device_service_url, payload.live_substream_profile_token, payload.camera_brand,
        )
        safe_stream_url(substream.stream_url)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="The selected live substream could not be resolved") from exc
    substream_capability = await probe_stream(substream.stream_url)
    if not substream_capability.get("reachable"):
        raise HTTPException(status_code=422, detail="The selected live substream failed its video test")
    main_video = capability.get("video") or {}
    sub_video = substream_capability.get("video") or {}
    main_pixels = int(main_video.get("width") or 0) * int(main_video.get("height") or 0)
    sub_pixels = int(sub_video.get("width") or 0) * int(sub_video.get("height") or 0)
    if not main_pixels or not sub_pixels or sub_pixels >= main_pixels:
        raise HTTPException(status_code=422, detail="Choose an ONVIF profile with a lower video resolution for grid viewing")
    return substream.stream_url


async def _resolve_live_substream(payload: CameraCreateRequest, resolved_service_url: str | None, capability: dict[str, Any], video_problem: str | None) -> str | None:
    """The lower-resolution stream chosen for grid viewing, checked: it resolves, opens, and is smaller than the main stream. A camera saved anyway uses the main stream instead."""
    try:
        if not payload.live_substream_profile_token or video_problem is not None:
            return None
        return await _check_live_substream(payload, resolved_service_url, capability)
    except HTTPException:
        if not payload.save_anyway:
            raise
        return None  # Live view uses the main stream


def _store_new_camera(discovery_claim: dict[str, Any] | None, payload: CameraCreateRequest, *, name: str, stream_url: str, resolved_service_url: str | None, capability: dict[str, Any],
                      video_problem: str | None, live_substream_url: str | None, motion_username: str | None, motion_password: str | None, health: Any,
                      public_address: str | None = None, actor_id: int | None = None) -> str:
    """Insert the camera with its capabilities, and (for a discovered device) claim its candidate in the same transaction. Returns the new camera's id."""
    with connect() as db:
        try:
            if discovery_claim:
                db.execute("BEGIN IMMEDIATE")
            camera_id = insert_camera(
                db, name=name, stream_url=stream_url, recording_mode=payload.recording_mode,
                capability=capability, onvif_uuid=payload.onvif_uuid,
                camera_ip=payload.camera_ip, camera_brand=payload.camera_brand,
                device_service_url=resolved_service_url or payload.device_service_url,
                onvif_profile_token=payload.onvif_profile_token,
                live_substream_profile_token=payload.live_substream_profile_token if live_substream_url else None,
                recording_enabled=payload.recording_enabled and video_problem is None,
                motion_service_url=payload.motion_service_url or resolved_service_url,
                motion_username=motion_username,
                motion_password=motion_password,
                motion_pre_roll_seconds=payload.motion_pre_roll_seconds,
                motion_post_roll_seconds=payload.motion_post_roll_seconds,
                motion_fallback_enabled=payload.motion_fallback_enabled,
                motion_zones=payload.motion_zones,
                motion_sensitivity=payload.motion_sensitivity,
                live_substream_url=live_substream_url,
                public_address=public_address,
            )
            if public_address == "allowed":
                audit_record(db, actor_id, "camera_public_address_allowed", "camera", camera_id, change="added")
            if video_problem is None:
                record_discovery_probe(
                    db,
                    discovery_claim["device_uuid"] if discovery_claim else payload.onvif_uuid,
                    capability,
                )
            _record_new_camera_capabilities(db, camera_id, payload, capability, video_problem, bool(payload.motion_service_url or resolved_service_url), health)
            if discovery_claim:
                claimed = db.execute(
                    "UPDATE discovery_candidates SET linked_camera_id=?,reviewed_at=?,add_claim_token=NULL,add_claimed_until=NULL "
                    "WHERE id=? AND add_claim_token=? AND linked_camera_id IS NULL",
                    (
                        camera_id,
                        datetime.now(timezone.utc).isoformat(),
                        discovery_claim["candidate_id"],
                        discovery_claim["token"],
                    ),
                )
                if claimed.rowcount != 1:
                    raise HTTPException(status_code=409, detail="This discovered device was linked or claimed by another request")
                db.commit()
        except sqlite3.IntegrityError as exc:
            if discovery_claim:
                db.rollback()
            raise HTTPException(status_code=409, detail="A camera with that name or ONVIF identity already exists") from exc
        except Exception:
            if discovery_claim:
                db.rollback()
            raise
    return camera_id


async def add_camera(payload: CameraCreateRequest, recorders: Any, *, discovery_claim: dict[str, Any] | None = None, allow_public: bool = False,
                     actor_id: int | None = None) -> dict[str, object]:
    """Check a new camera's stream and save it, then start recording it if asked. This is everything ``POST /api/cameras`` does, without a web request, so the
    bulk-add job (``app.bulk_add``) adds a camera exactly as the form does. Raises ``HTTPException`` (422 for what is wrong with the camera, 409 for a name in use).
    ``allow_public``: an administrator in a browser ticked that the camera is at a public address (``public_permission``); Bulk Add never does (#474)."""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Camera name is required")
    with camera_destination.public_permitted(allow_public):
        public = await check_destinations(payload, allow_public=allow_public)
        return await _add_camera(payload, recorders, name, discovery_claim, "allowed" if public else None, actor_id)


async def _add_camera(payload: CameraCreateRequest, recorders: Any, name: str, discovery_claim: dict[str, Any] | None, public_address: str | None,
                      actor_id: int | None) -> dict[str, object]:
    onvif_resolved = not payload.stream_url and bool(payload.camera_ip)
    stream_url, resolved_service_url, unresolved = await _resolve_new_camera_source(payload, onvif_resolved)
    motion_username = payload.motion_username if payload.motion_username is not None else payload.camera_username
    motion_password = payload.motion_password if payload.motion_password is not None else payload.camera_password
    if bool(motion_username) != bool(motion_password):
        raise HTTPException(status_code=422, detail="Provide both ONVIF motion username and password")
    capability, video_problem = await _probe_new_camera_stream(payload, stream_url, unresolved)
    live_substream_url = await _resolve_live_substream(payload, resolved_service_url, capability, video_problem)
    health = await _initial_health(payload, resolved_service_url) if onvif_resolved else None
    camera_id = await asyncio.to_thread(_store_new_camera, discovery_claim, payload, name=name, stream_url=stream_url, resolved_service_url=resolved_service_url, capability=capability, video_problem=video_problem,
                                  live_substream_url=live_substream_url, motion_username=motion_username, motion_password=motion_password, health=health,
                                  public_address=public_address, actor_id=actor_id)
    await recorders.sync_camera(camera_id, force_restart=True)
    return await asyncio.to_thread(_saved_view, camera_id)


@router.post("/api/cameras", status_code=status.HTTP_201_CREATED)
async def create_camera(
    payload: CameraCreateRequest,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    return await add_camera(payload, request.app.state.recorders, discovery_claim=getattr(request.state, "discovery_add_claim", None),
                            allow_public=public_permission(payload, user), actor_id=user.id)


def _url_host(url: str | None) -> str | None:
    """The host of an ONVIF address, spelled as camera addresses are stored (lower case, no brackets), or None."""
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
        return normalise_host(host) if host else None
    except ValueError:
        return None


def _login_hosts(row: sqlite3.Row) -> set[str]:
    """The hosts a camera's saved ONVIF login has been given to: its address, and the hosts of its device and event addresses.

    The login is sent to whatever device and event addresses are stored (PTZ, masks, health, backfill, motion events). An address moved to
    any other host must not take the saved login with it: whoever runs that host could then see a digest of the password, which can be
    guessed offline (#472). Moving within one of these hosts (another path or port) keeps it."""
    hosts = {_url_host(row["device_service_url"]), _url_host(row["motion_service_url"])}
    if row["camera_ip"]:
        hosts.add(normalise_host(row["camera_ip"]))
    return {host for host in hosts if host}


def _reuse_source_credentials(current: sqlite3.Row, payload: CameraSourceUpdateRequest) -> CameraSourceUpdateRequest:
    if payload.camera_ip and payload.camera_ip == current["camera_ip"] and not payload.clear_credentials:
        try:
            existing = urlsplit(decrypt(current["stream_url_ciphertext"]))
            existing_user = unquote(existing.username or "")
            existing_password = unquote(existing.password or "")
        except (RuntimeError, ValueError):
            existing_user = existing_password = ""
        updates = {}
        if not payload.camera_username and existing_user:
            updates["camera_username"] = existing_user
        if not payload.camera_password and existing_password:
            updates["camera_password"] = existing_password
        if updates:
            payload = payload.model_copy(update=updates)
    if bool(payload.camera_username) != bool(payload.camera_password):
        raise HTTPException(status_code=422, detail="Provide both camera username and password, or leave both blank")
    return payload


def _saved_view(camera_id: str) -> dict[str, object]:
    """The camera as it is stored now, for the answer to a change. Run in a thread by the asynchronous handlers (#354)."""
    with connect() as db:
        row = db.execute("SELECT c.*,g.name AS group_name FROM cameras c LEFT JOIN camera_groups g ON g.id=c.group_id WHERE c.id=?", (camera_id,)).fetchone()
    return camera_view(row)


@router.post("/api/cameras/{camera_id}/source/test")
async def test_camera_source_update(
    camera_id: str,
    payload: CameraSourceUpdateRequest,
    user: Administrator,
) -> dict[str, object]:
    current = await asyncio.to_thread(read_one, "SELECT camera_ip,stream_url_ciphertext FROM cameras WHERE id=?", (camera_id,))
    if current is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    payload = _reuse_source_credentials(current, payload)
    allowed = public_permission(payload, user)
    await check_destinations(payload, allow_public=allowed)
    started = time.monotonic()
    with camera_destination.public_permitted(allowed):
        try:
            stream_url, _ = await _resolve_camera_source(payload)
            safe_stream_url(stream_url)
        except (ValueError, HTTPException) as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result = await probe_stream(stream_url)
    await asyncio.to_thread(diagnostics.record_event, "stream_probe", outcome="success" if result.get("reachable") else "unavailable", elapsed_ms=min(60000, int((time.monotonic() - started) * 1000)))
    return result


@router.post("/api/cameras/{camera_id}/source/profiles")
async def list_camera_source_update_profiles(
    camera_id: str,
    payload: CameraSourceUpdateRequest,
    user: Administrator,
) -> dict[str, object]:
    if not payload.camera_ip:
        raise HTTPException(status_code=422, detail="Enter the camera IP address before loading ONVIF profiles")
    current = await asyncio.to_thread(read_one, "SELECT camera_ip,stream_url_ciphertext FROM cameras WHERE id=?", (camera_id,))
    if current is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    payload = _reuse_source_credentials(current, payload)
    allowed = public_permission(payload, user)
    await check_destinations(payload, allow_public=allowed)
    try:
        with camera_destination.public_permitted(allowed):
            service_url, profiles = await list_camera_stream_profiles(
                payload.camera_ip, payload.camera_username or "", payload.camera_password or "",
                payload.device_service_url, payload.camera_brand,
            )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"device_service_url": service_url, "profiles": [profile.__dict__ for profile in profiles]}


@router.put("/api/cameras/{camera_id}/source")
async def update_camera_source(
    camera_id: str,
    payload: CameraSourceUpdateRequest,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    current = await asyncio.to_thread(read_one, "SELECT camera_ip,stream_url_ciphertext,device_service_url,motion_service_url,motion_credentials_ciphertext,public_address "
                                                "FROM cameras WHERE id=?", (camera_id,))
    if current is None:
        raise HTTPException(status_code=404, detail="Camera not found")

    entered = (payload.camera_username, payload.camera_password) if payload.camera_ip and payload.camera_username and payload.camera_password else None
    payload = _reuse_source_credentials(current, payload)
    # A new source at a public address needs the tick, whatever the camera had before, one kept from before the rule (grandfathered) too (#474)
    allowed = public_permission(payload, user)
    public = await check_destinations(payload, allow_public=allowed)

    with camera_destination.public_permitted(allowed):
        try:
            stream_url, resolved_service_url = await _resolve_camera_source(payload)
            safe_stream_url(stream_url)
        except (ValueError, HTTPException) as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        capability = await probe_stream(stream_url)
        if not capability.get("reachable"):
            raise HTTPException(status_code=422, detail=str(capability.get("reason", "Camera stream could not be opened")))

        live_substream_url = await _check_live_substream(payload, resolved_service_url, capability) if payload.live_substream_profile_token else None

    selected_service_url = resolved_service_url or payload.device_service_url
    # The saved ONVIF login goes only where it has gone before (#472). An ONVIF address on another host takes the login entered with this
    # change, as adding a camera does, or none: it is cleared, and the camera's ONVIF login is entered again under Motion source.
    new_host = _url_host(selected_service_url)
    moved = bool(new_host and current["motion_credentials_ciphertext"] and new_host not in _login_hosts(current))
    onvif_login = "kept" if not moved else ("replaced" if entered else "cleared")
    def save():
        with connect() as db:
            if moved:
                db.execute("UPDATE cameras SET motion_credentials_ciphertext=? WHERE id=?",
                           (encrypt(json.dumps({"username": entered[0], "password": entered[1]}, separators=(",", ":"))) if entered else None, camera_id))
            audit_record(db, user.id, "camera_source_changed", "camera", camera_id, device_host=new_host, onvif_login=onvif_login)
            if public:
                audit_record(db, user.id, "camera_public_address_allowed", "camera", camera_id, change="source", before=current["public_address"])
            db.execute("UPDATE cameras SET public_address=? WHERE id=?", ("allowed" if public else None, camera_id))
            request_mask_check(db, camera_id)  # A new source or login: ask the camera for its privacy masks at the next pass (#468)
            db.execute(
                "UPDATE cameras SET camera_ip=?,camera_brand=?,device_service_url=?,onvif_profile_token=?,live_substream_profile_token=?,"
                "motion_service_url=COALESCE(?,motion_service_url),"
                "stream_url_ciphertext=?,live_substream_url_ciphertext=?,audio_supported=?,audio_enabled=CASE WHEN ?=0 THEN 0 ELSE audio_enabled END,"
                "video_codec=?,video_width=?,video_height=?,last_probe_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),last_error=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id=?",
                (payload.camera_ip, payload.camera_brand, selected_service_url, payload.onvif_profile_token,
                 payload.live_substream_profile_token, selected_service_url, encrypt(stream_url.strip()),
                 encrypt(live_substream_url.strip()) if live_substream_url else None,
                 int(bool(capability.get("audio"))), int(bool(capability.get("audio"))),
                 (capability.get("video") or {}).get("codec"), (capability.get("video") or {}).get("width"),
                 (capability.get("video") or {}).get("height"), camera_id),
            )
            camera_capabilities.record(db, camera_id, "video", "ok", "Video is flowing")
            camera_capabilities.record(db, camera_id, "audio", "available" if capability.get("audio") else "unsupported", "The camera carries audio" if capability.get("audio") else "No audio on the stream")

    await asyncio.to_thread(save)
    if not capability.get("audio"):
        privacy.audio_turned_off(camera_id)  # The new stream has no audio, so audio is off for this camera: open streams carrying it end (#489)
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return await asyncio.to_thread(_saved_view, camera_id)


@router.patch("/api/cameras/{camera_id}")
async def update_camera(
    camera_id: str,
    payload: CameraUpdateRequest,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    if "name" in payload.model_fields_set and payload.name is None:
        raise HTTPException(status_code=422, detail="Camera name is required")
    if not any(name in payload.model_fields_set for name in ("name", "recording_mode", "recording_enabled", "motion_pre_roll_seconds", "motion_post_roll_seconds", "audio_enabled")):
        raise HTTPException(status_code=422, detail="Specify a camera name, recording setting, buffer duration, or audio setting")
    def save():
        with connect() as db:
            row = db.execute("SELECT recording_mode,recording_enabled,motion_service_url,motion_fallback_enabled,audio_supported,audio_enabled FROM cameras WHERE id=?", (camera_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Camera not found")
            if payload.audio_enabled and row["audio_supported"] is not True and row["audio_supported"] != 1:
                message = "Probe the camera audio capability before enabling audio" if row["audio_supported"] is None else "This camera stream has no supported audio track"
                raise HTTPException(status_code=422, detail=message)
            name_only = payload.model_fields_set == {"name"}
            mode = payload.recording_mode or row["recording_mode"]
            enabled = payload.recording_enabled if payload.recording_enabled is not None else bool(row["recording_enabled"])
            if mode == "disabled":
                enabled = False
            if name_only:
                assignments = ["name=?", "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
                values: list[object] = [payload.name]
            else:
                assignments = ["recording_mode=?", "recording_enabled=?", "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
                values = [mode, int(enabled)]
                if payload.name is not None:
                    assignments.append("name=?")
                    values.append(payload.name)
            audio_enabled = payload.audio_enabled if payload.audio_enabled is not None else bool(row["audio_enabled"])
            if not name_only:
                if payload.audio_enabled is not None:
                    assignments.append("audio_enabled=?"); values.append(int(audio_enabled))
                    if bool(row["audio_enabled"]) != audio_enabled:
                        audit_record(db, int(user["id"]), "camera_audio_enabled" if audio_enabled else "camera_audio_disabled", "camera", camera_id, audio_supported=bool(row["audio_supported"]))
                if payload.motion_pre_roll_seconds is not None:
                    assignments.append("motion_pre_roll_seconds=?"); values.append(payload.motion_pre_roll_seconds)
                if payload.motion_post_roll_seconds is not None:
                    assignments.append("motion_post_roll_seconds=?"); values.append(payload.motion_post_roll_seconds)
                if not enabled:
                    assignments.extend(("recording_status='stopped'", "last_error=NULL"))
                elif mode == "motion" and not row["motion_service_url"] and not row["motion_fallback_enabled"]:
                    assignments.extend(("recording_status='unsupported'", "last_error='Configure an ONVIF event service or enable server motion fallback'"))
                else:
                    assignments.extend(("recording_status='connecting'", "last_error=NULL"))
            values.append(camera_id)
            try:
                db.execute(f"UPDATE cameras SET {','.join(assignments)} WHERE id=?", values)
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="A camera with that name already exists") from exc
        return name_only

    name_only = await asyncio.to_thread(save)
    if payload.audio_enabled is False:
        privacy.audio_turned_off(camera_id)  # Open streams carrying this camera's audio end; their viewers come back without it (#489)
    if not name_only:
        await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return await asyncio.to_thread(_saved_view, camera_id)


@router.post("/api/cameras/{camera_id}/credentials")
async def rotate_camera_credentials(
    camera_id: str,
    payload: CameraCredentialRotateRequest,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    # The request carries a whole source, so it is held to the rule as a source change is: a public address needs the tick (#474)
    allowed = public_permission(payload, user)
    public = await check_destinations(payload, allow_public=allowed)
    with camera_destination.public_permitted(allowed):
        try:
            stream_url, _ = await _resolve_camera_source(payload)
            safe_stream_url(stream_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        camera = await asyncio.to_thread(read_one, "SELECT id,recording_mode FROM cameras WHERE id=?", (camera_id,))
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        capability = await probe_stream(stream_url)
    if not capability.get("reachable"):
        raise HTTPException(status_code=422, detail=str(capability.get("reason", "Updated camera stream could not be opened")))
    video = capability["video"]
    def save():
        with connect() as db:
            db.execute(
                "UPDATE cameras SET stream_url_ciphertext=?,live_substream_url_ciphertext=NULL,audio_supported=?,audio_enabled=CASE WHEN ?=0 THEN 0 ELSE audio_enabled END,video_codec=?,video_width=?,video_height=?,last_probe_at=?,last_error=NULL,updated_at=? WHERE id=?",
                (encrypt(stream_url), int(bool(capability.get("audio"))), int(bool(capability.get("audio"))), video["codec"], video["width"], video["height"], datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), camera_id),
            )
            db.execute("UPDATE cameras SET public_address=? WHERE id=?", ("allowed" if public else None, camera_id))
            if public:
                audit_record(db, user.id, "camera_public_address_allowed", "camera", camera_id, change="credentials")
            request_mask_check(db, camera_id)  # New login: ask the camera for its privacy masks at the next pass (#468)

    await asyncio.to_thread(save)
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return await asyncio.to_thread(_saved_view, camera_id)


@router.post("/api/cameras/{camera_id}/audio/probe")
async def probe_camera_audio(
    camera_id: str,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    camera = await asyncio.to_thread(read_one, "SELECT stream_url_ciphertext,audio_enabled,public_address FROM cameras WHERE id=?", (camera_id,))
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    try:
        with camera_destination.public_permitted(camera_destination.camera_allows_public(camera)):  # The camera address rule (#474)
            capability = await probe_stream(decrypt(camera["stream_url_ciphertext"]))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Camera stream configuration is unavailable") from exc
    if not capability.get("reachable"):
        raise HTTPException(status_code=503, detail=str(capability.get("reason", "Camera stream could not be opened")))
    supported = bool(capability.get("audio"))
    def save():
        with connect() as db:
            db.execute("UPDATE cameras SET audio_supported=?,audio_enabled=CASE WHEN ?=0 THEN 0 ELSE audio_enabled END,last_probe_at=?,updated_at=? WHERE id=?",
                       (int(supported), int(supported), datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), camera_id))
            if not supported and camera["audio_enabled"]:
                audit_record(db, int(user["id"]), "camera_audio_disabled", "camera", camera_id, reason="audio_capability_lost")

    await asyncio.to_thread(save)
    if not supported and camera["audio_enabled"]:
        # If the recorder was retaining an audio track, restart it with the safe muted setting.
        await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return {"audio_supported": supported, "audio_enabled": bool(camera["audio_enabled"] and supported), "audio_status": "available_disabled" if supported and not camera["audio_enabled"] else "enabled" if supported else "unsupported", "detail": "AAC is used in recordings; live listening is served as MP3 for broad browser playback."}


@router.post("/api/cameras/{camera_id}/motion-source")
async def configure_motion_source(
    camera_id: str,
    payload: MotionSourceUpdateRequest,
    request: Request,
    user: Administrator,
) -> dict[str, object]:
    credentials_supplied = payload.username is not None or payload.password is not None
    if credentials_supplied and bool(payload.username) != bool(payload.password):
        raise HTTPException(status_code=422, detail="Provide both ONVIF motion username and password")
    if payload.service_url:
        # The event address is held to the camera address rule (#474), with the camera's own permission for a public address
        camera = await asyncio.to_thread(read_one, "SELECT public_address FROM cameras WHERE id=?", (camera_id,))
        if camera is None:
            raise HTTPException(status_code=404, detail="Camera not found")
        await check_destinations(SimpleNamespace(motion_service_url=payload.service_url), allow_public=camera_destination.camera_allows_public(camera))
    def save():
        with connect() as db:
            row = db.execute("SELECT recording_mode,recording_enabled,motion_service_url,motion_fallback_enabled,motion_zones_json,motion_sensitivity,motion_pre_roll_seconds,motion_post_roll_seconds,"
                             "camera_ip,device_service_url,motion_credentials_ciphertext FROM cameras WHERE id=?", (camera_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Camera not found")
            service_url = payload.service_url if "service_url" in payload.model_fields_set else row["motion_service_url"]
            new_host = _url_host(service_url)
            if new_host and new_host not in _login_hosts(row) and row["motion_credentials_ciphertext"] and not credentials_supplied:
                # The saved login would go to a host it has never gone to (#472): ask for it again rather than send it.
                raise HTTPException(status_code=422, detail="This event address is on another host than the camera's. Enter the ONVIF username and password "
                                                            "again, so the saved login is not sent to a different device.")
            fallback_enabled = payload.fallback_enabled if payload.fallback_enabled is not None else bool(row["motion_fallback_enabled"])
            if not service_url and not fallback_enabled:
                raise HTTPException(status_code=422, detail="Configure an ONVIF event service or enable server motion fallback")
            assignments = ["motion_service_url=?", "motion_pre_roll_seconds=?", "motion_post_roll_seconds=?", "motion_source_status=?"]
            pre_roll_seconds = payload.pre_roll_seconds if payload.pre_roll_seconds is not None else row["motion_pre_roll_seconds"]
            post_roll_seconds = payload.post_roll_seconds if payload.post_roll_seconds is not None else row["motion_post_roll_seconds"]
            values: list[object] = [service_url, pre_roll_seconds, post_roll_seconds, "untested" if service_url else "unsupported"]
            if credentials_supplied:
                assignments.append("motion_credentials_ciphertext=?")
                values.append(encrypt(json.dumps({"username": payload.username or "", "password": payload.password or ""}, separators=(",", ":"))))
            zones = payload.zones if payload.zones is not None else [MotionZoneRequest.model_validate(zone) for zone in json.loads(row["motion_zones_json"])]
            sensitivity = payload.sensitivity if payload.sensitivity is not None else row["motion_sensitivity"]
            assignments.extend(("motion_fallback_enabled=?", "motion_zones_json=?", "motion_sensitivity=?", "server_motion_status=?"))
            values.extend((int(fallback_enabled), json.dumps([zone.model_dump() for zone in zones], separators=(",", ":")), sensitivity, "waiting" if fallback_enabled else "disabled"))
            assignments.extend(("recording_status=CASE WHEN recording_enabled=1 AND recording_mode='motion' THEN 'connecting' ELSE recording_status END", "last_error=NULL", "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"))
            values.append(camera_id)
            db.execute(
                f"UPDATE cameras SET {','.join(assignments)} WHERE id=?", values,
            )
            # Recorded once the change is certain to be made. The address holds no login (the request refuses one with credentials in it).
            audit_record(db, user.id, "camera_motion_source_changed", "camera", camera_id, event_host=new_host,
                         onvif_login="replaced" if credentials_supplied else "kept", fallback=bool(fallback_enabled))
            if credentials_supplied:
                request_mask_check(db, camera_id)  # A new ONVIF login: ask the camera for its privacy masks at the next pass (#468)

    await asyncio.to_thread(save)
    await request.app.state.recorders.sync_camera(camera_id, force_restart=True)
    return await asyncio.to_thread(_saved_view, camera_id)
