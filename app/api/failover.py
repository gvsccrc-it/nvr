"""Failover recorder (#143): the administrator's settings and status, the status for everyone, and the signed endpoints the two NVRs call on each other.

The signed endpoints are on ``inbound_router``: the web interface serves them (for a pair that allows an unencrypted link, or one reached
through a proxy over HTTPS), and so does the failover link's own HTTPS listener (``app.failover_listener``, #502 part 2)."""
from __future__ import annotations

import json
import time
from collections import deque
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from app import failover, failover_link, network
from app.audit import record
from app.credentials import encrypt
from app.principal import Administrator, CurrentUser
from app.store import connect

router = APIRouter()
inbound_router = APIRouter()  # The endpoints the two NVRs call on each other: also served alone by the failover link's listener


class FailoverInput(BaseModel):
    role: Literal["off", "primary", "standby"]
    peer_url: str | None = Field(default=None, max_length=2048)
    secret: str | None = Field(default=None, max_length=256)  # Write-only: left out, the saved one is kept
    allow_private: bool = False
    # The link (#502, part 2): the other NVR's certificate fingerprint for an https:// address, and "Allow an unencrypted link"
    peer_fingerprint: str | None = Field(default=None, max_length=200)
    allow_unencrypted: bool = False
    silence_seconds: int = Field(default=120, ge=10, le=3600)
    lease_seconds: int = Field(default=60, ge=5, le=1800)
    sync_seconds: int = Field(default=900, ge=60, le=86400)

    @field_validator("secret")
    @classmethod
    def long_enough(cls, value: str | None) -> str | None:
        if value is not None and len(value) < failover.MIN_SECRET_LENGTH:
            raise ValueError(f"The failover secret must be at least {failover.MIN_SECRET_LENGTH} characters")
        return value

    @model_validator(mode="after")
    def consistent(self) -> "FailoverInput":
        if self.silence_seconds < self.lease_seconds * 2:
            raise ValueError("The standby must wait at least twice the lease before it takes over, so a lease it was granted has always run out")
        return self


def _link() -> dict[str, Any]:
    """This NVR's side of the link: the fingerprint the other NVR is to pin, and where the link is served. The certificate is made the first
    time this is asked for; its private key is never part of an answer."""
    from app.failover_listener import link_port

    _, fingerprint = failover_link.certificate()
    return {"fingerprint": failover_link.display(fingerprint), "port": link_port()}


def _view(db) -> dict[str, Any]:
    settings = failover.read_settings(db)
    settings = {**settings, "peer_fingerprint": failover_link.display(settings["peer_fingerprint"])}
    return {"settings": settings, "status": failover.status_view(db), "episodes": failover.episodes(db), "link": _link()}


@router.get("/api/admin/failover")
def get_failover(_: Administrator) -> dict[str, Any]:
    with connect() as db:
        return _view(db)


@router.put("/api/admin/failover")
def put_failover(body: FailoverInput, user: Administrator) -> dict[str, Any]:
    peer, fingerprint = None, None
    if body.role != "off":
        if not body.peer_url:
            raise HTTPException(status_code=422, detail="Enter the address of the other NVR")
        try:
            peer = failover.check_peer_url(body.peer_url, body.allow_private)
            fingerprint = failover_link.normalize_fingerprint(body.peer_fingerprint) if (body.peer_fingerprint or "").strip() else None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if fingerprint and not peer.lower().startswith("https://"):
            raise HTTPException(status_code=422, detail="A certificate fingerprint is for the other NVR's https:// failover link address")
        if fingerprint and fingerprint == failover_link.certificate()[1]:
            raise HTTPException(status_code=422, detail="That is this NVR's own fingerprint: enter the one shown on the other NVR's failover page")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            current, state = failover.read_settings(db), failover.read_state(db)
            # A pair set up before #502 keeps working as it was while its address stays the same; a new address follows the new rules.
            legacy = current["legacy_link"] and body.role != "off" and peer == current["peer_url"]
            if peer and peer.lower().startswith("http://") and not (body.allow_unencrypted or legacy):
                raise HTTPException(status_code=422, detail=failover.UNENCRYPTED_OUT)
            # Every new https:// pair pins the other NVR's certificate (the owner's decision on #502, 24 Nov): no public certificate authority
            # is trusted for the link. A pair saved before this without one keeps working while its address stays the same.
            kept_unpinned = current["role"] != "off" and peer == current["peer_url"] and not current["peer_fingerprint"]
            if peer and peer.lower().startswith("https://") and not fingerprint and not kept_unpinned:
                raise HTTPException(status_code=422, detail=failover.FINGERPRINT_REQUIRED)
            if body.role != "off" and not body.secret and not current["has_secret"]:
                raise HTTPException(status_code=422, detail=f"Enter a failover secret of at least {failover.MIN_SECRET_LENGTH} characters, the same on both NVRs")
            if current["role"] == "standby" and body.role != "standby" and (state["takeover_active"] or db.execute("SELECT 1 FROM failover_episodes WHERE role='standby' AND state='handback'").fetchone()):
                raise HTTPException(status_code=409, detail="This standby is recording in the primary's place or handing back: let the handback finish first")
            db.execute("UPDATE failover_settings SET role=?,peer_url=?,allow_private=?,silence_seconds=?,lease_seconds=?,sync_seconds=?,peer_fingerprint=?,allow_unencrypted=?,legacy_link=?,"
                       "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE singleton_id=1",
                       (body.role, peer, int(body.allow_private), body.silence_seconds, body.lease_seconds, body.sync_seconds, fingerprint,
                        int(body.allow_unencrypted and body.role != "off"), int(bool(legacy))))
            if body.secret:
                db.execute("UPDATE failover_settings SET secret_ciphertext=? WHERE singleton_id=1", (encrypt(body.secret),))
            if body.role == "standby" and current["role"] != "standby":
                failover._set_state(db, watching_since=failover._stamp(), last_renewed_at=None, takeover_active=0, takeover_started_at=None, yield_until=None, last_error=None, peer_state=None)
            elif body.role != "standby":
                failover._set_state(db, takeover_active=0, yield_until=None)
            record(db, int(user["id"]), "failover_settings_changed", "failover", "1", role=body.role, peer=peer, secret_changed=bool(body.secret),
                   peer_fingerprint=failover_link.display(fingerprint), allow_unencrypted=bool(body.allow_unencrypted and body.role != "off"), kept_from_upgrade=bool(legacy))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return _view(db)


@router.post("/api/admin/failover/link-certificate")
def replace_link_certificate(user: Administrator) -> dict[str, Any]:
    """Make a new certificate for this NVR's failover link (#502). The other NVR refuses the link until it is paired again with the new
    fingerprint; the listener serves the new certificate within a few seconds."""
    _, fingerprint = failover_link.certificate(replace=True)
    with connect() as db:
        record(db, int(user["id"]), "failover_link_certificate_replaced", "failover", "1", fingerprint=failover_link.display(fingerprint))
        return _view(db)


@router.post("/api/admin/failover/sync", status_code=202)
def sync_now(user: Administrator) -> dict[str, Any]:
    """A standby fetches the primary's configuration now (at the manager's next pass)."""
    with connect() as db:
        if failover.read_settings(db)["role"] != "standby":
            raise HTTPException(status_code=409, detail="Only a standby copies the primary's configuration")
        failover._set_state(db, sync_requested=1)
        record(db, int(user["id"]), "failover_sync_requested", "failover", "1")
        return {"requested": True}


@router.get("/api/failover/status")
def get_status(_: CurrentUser) -> dict[str, Any]:
    """Where failover stands, for everyone who is signed in (the status area shows it)."""
    with connect() as db:
        return failover.status_view(db)


# --- the signed endpoints the two NVRs call on each other (the signature is the credential, not a sign-in) ----------------------------------------

MAX_BODY_BYTES = 4096
MAX_FAILURES_PER_MINUTE = 20
_failures: dict[str, deque[float]] = {}


def _blocked(host: str) -> bool:
    """Whether an address has sent too many badly signed requests in the last minute. Its further bad requests get 429. A correctly signed
    request is never refused for this: the count is by address, and the primary may share one with others behind a proxy or NAT, who could
    otherwise shut out its renewals and make a healthy primary look silent (#343)."""
    now = time.monotonic()
    window = _failures.setdefault(host, deque())
    while window and now - window[0] > 60:
        window.popleft()
    if len(_failures) > 1000:
        for key in [key for key, times in _failures.items() if not times or now - times[-1] > 60]:
            del _failures[key]
    return len(window) >= MAX_FAILURES_PER_MINUTE


def _target(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


async def _signed(request: Request) -> bytes:
    host = request.client.host if request.client else ""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise failover.PeerError(413, "The request is too large")
    body = bytearray()
    async for chunk in request.stream():  # Capped as it is read: a chunked body declares no length, and nothing is checked yet
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise failover.PeerError(413, "The request is too large")
    body = bytes(body)
    try:
        await run_in_threadpool(failover.authenticate, request.method, _target(request), body, request.headers)
    except failover.PeerError:
        if _blocked(host):
            raise failover.PeerError(429, "Too many refused requests") from None
        _failures.setdefault(host, deque()).append(time.monotonic())
        raise
    if network.scheme(request) != "https":  # Arrived unencrypted: answered only while this NVR allows an unencrypted link (#502)
        await run_in_threadpool(failover.refuse_unencrypted)
    return body


def _refusal(exc: failover.PeerError, request: Request | None = None, secret: str | None = None) -> Response:
    """A refusal. Given the authenticated request and the secret, it is signed as an answer to that request, so the primary can believe it
    (a "no such footage" makes the primary skip that footage, #502); a refusal of a request that was not authenticated is never signed."""
    body = json.dumps({"detail": exc.detail}, separators=(",", ":")).encode()
    headers = failover.sign_answer(secret, _target(request), body, request.headers.get("x-failover-signature", "")) if request is not None and secret else {}
    return Response(body, status_code=exc.status, media_type="application/json", headers=headers)


async def _answer(request: Request, payload: Any) -> Response:
    """A JSON answer to an authenticated request, signed as the answer to it (#343, #502)."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    secret = await run_in_threadpool(_shared_secret)
    return Response(body, media_type="application/json", headers=failover.sign_answer(secret or "", _target(request), body, request.headers.get("x-failover-signature", "")))


def _epoch(request: Request) -> int:
    try:
        return int(request.query_params.get("epoch", ""))
    except ValueError:
        raise failover.PeerError(422, "An epoch is needed") from None


def _shared_secret() -> str | None:
    with connect() as db:
        return failover.shared_secret(db)


@inbound_router.post("/api/inbound/failover/lease")
async def lease(request: Request) -> Response:
    """The primary renews its lease. The standby's answer says whether it is granted and whether footage waits to be brought back."""
    try:
        await _signed(request)
        # Signed, and bound to the request it answers: the primary yields only to an answer it can prove came from its standby.
        return await _answer(request, await run_in_threadpool(failover.standby_lease, {}))
    except failover.PeerError as exc:
        return _refusal(exc)


async def _authenticated(request: Request) -> tuple[bytes, str | None]:
    """The body of a request that passed ``_signed``, and the secret to sign the answer (and any refusal) with."""
    body = await _signed(request)
    return body, await run_in_threadpool(_shared_secret)


@inbound_router.post("/api/inbound/failover/handback-complete")
async def handback_complete(request: Request) -> Response:
    secret = None
    try:
        body, secret = await _authenticated(request)
        try:
            payload = json.loads(body or b"{}")
            epoch, imported, size = int(payload["epoch"]), int(payload.get("imported", 0)), int(payload.get("bytes", 0))
        except (ValueError, KeyError, TypeError):
            raise failover.PeerError(422, "The request is not understood") from None
        await run_in_threadpool(failover.standby_handback_complete, epoch, imported, size)
        return await _answer(request, {"done": True})  # The primary marks the handback done only on this signed answer (#502)
    except failover.PeerError as exc:
        return _refusal(exc, request, secret)


@inbound_router.get("/api/inbound/failover/footage")
async def footage(request: Request) -> Response:
    secret = None
    try:
        _, secret = await _authenticated(request)
        return await _answer(request, {"segments": await run_in_threadpool(failover.standby_footage, _epoch(request))})
    except failover.PeerError as exc:
        return _refusal(exc, request, secret)


@inbound_router.get("/api/inbound/failover/footage/{segment_id}")
async def footage_file(segment_id: int, request: Request):
    secret = None
    try:
        _, secret = await _authenticated(request)
        path, digest = await run_in_threadpool(failover.standby_footage_file, _epoch(request), segment_id)
        size = (await run_in_threadpool(path.stat)).st_size
    except failover.PeerError as exc:
        return _refusal(exc, request, secret)
    except OSError:
        return _refusal(failover.PeerError(404, "The footage is not available"), request, secret)
    # The file's digest and size are signed as the answer to this request; the primary checks the bytes it receives against them (#502).
    signed = failover.sign_answer(secret or "", _target(request), failover.footage_summary(digest, size), request.headers.get("x-failover-signature", ""))
    return FileResponse(path, media_type="video/mp4", headers={"X-Footage-Sha256": digest, "X-Footage-Size": str(size), "Cache-Control": "no-store", **signed})


@inbound_router.get("/api/inbound/failover/backup")
async def backup_file(request: Request):
    """The primary's newest encrypted backup, for the standby to copy the configuration from (it holds the credential key, so it is sealed with the backup passphrase)."""
    try:
        await _signed(request)
        path = await run_in_threadpool(failover.backup_for_standby)
    except failover.PeerError as exc:
        return _refusal(exc)
    return FileResponse(path, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})
