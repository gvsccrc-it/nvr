"""Signed footage: verify a camera's recordings against its signed hash chain."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta
from typing import Annotated, Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, StringConstraints

from app import footage_chain
from app.audit import record as audit_record
from app.principal import Administrator, AdministratorSession, CurrentUser, requires_scope
from app.store import connect

router = APIRouter()

# Who may verify how much, as decided on #508: verifying re-reads every file of the range (about 300 GB for a week of one 4 Mbit/s camera),
# and footage on a cold tier is downloaded first. It needs the export scope; an administrator may check up to a week, archived footage
# included; anyone else a day, of footage on the hot disk only (archived footage is "not checked (archived)"). Two run at once and one for
# each person; the rest wait in line and are told where they stand. Each stops between two files when its requester leaves, and each is in
# the security log.
ADMINISTRATOR_WINDOW = timedelta(days=7)
OTHERS_WINDOW = timedelta(hours=24)
AT_ONCE = 2
PROGRESS_SECONDS = 0.5


class _Turns:
    """Verifications under way and those waiting for a turn, in the order they asked. All on the web server's event loop."""

    def __init__(self, at_once: int) -> None:
        self.at_once = at_once
        self.running: set[int] = set()
        self.waiting: list[tuple[int, asyncio.Future[None]]] = []

    def has(self, person: int) -> bool:
        return person in self.running or any(waiting == person for waiting, _ in self.waiting)

    def ask(self, person: int) -> asyncio.Future[None]:
        turn: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if len(self.running) < self.at_once and not self.waiting:
            self.running.add(person)
            turn.set_result(None)
        else:
            self.waiting.append((person, turn))
        return turn

    def position(self, turn: asyncio.Future[None]) -> int:
        """1 for the next to start; 0 once it has started."""
        return next((index + 1 for index, (_, waiting) in enumerate(self.waiting) if waiting is turn), 0)

    def leave(self, person: int, turn: asyncio.Future[None]) -> None:
        for index, (_, waiting) in enumerate(self.waiting):
            if waiting is turn:
                del self.waiting[index]
                return
        self.running.discard(person)
        while len(self.running) < self.at_once and self.waiting:
            next_person, next_turn = self.waiting.pop(0)
            if next_turn.done():  # Given up while it waited
                continue
            self.running.add(next_person)
            next_turn.set_result(None)


_turns = _Turns(AT_ONCE)


def _bounds(user: Any, start: datetime, end: datetime) -> bool:
    """Check the range for this person; whether archived footage is read."""
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(status_code=422, detail="Times must include a timezone")
    administrator = user["role"] == "administrator"
    window = ADMINISTRATOR_WINDOW if administrator else OTHERS_WINDOW
    if not start < end or end - start > window:
        raise HTTPException(status_code=422, detail=f"Verify up to {'7 days' if administrator else '24 hours'} at a time")
    return administrator


def _camera_exists(camera_id: str, user: Any) -> None:
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None or not user.access.can_see(camera_id):
            raise HTTPException(status_code=404, detail="Camera not found")


def _verify_and_record(user: Any, camera_id: str, start: datetime, end: datetime, include_archived: bool, cancel: threading.Event,
                       progress: dict[str, int]) -> dict[str, Any]:
    """The check itself, in a thread, and its row in the security log whatever its outcome."""
    outcome, result = "failed", None
    try:
        result = footage_chain.verify(camera_id, start, end, include_archived=include_archived, cancel=cancel,
                                      progress=lambda done, total: progress.update(checked=done, total=total))
        outcome = "valid" if result["valid"] else "not_valid"
        return result
    except footage_chain.VerificationCancelled:
        outcome = "cancelled"
        raise
    finally:
        with connect() as db:
            audit_record(db, int(user["id"]), "footage_verified", "camera", camera_id, start=start.isoformat(), end=end.isoformat(), outcome=outcome,
                         archived_checked=include_archived, segments=len(result["segments"]) if result else progress.get("checked", 0),
                         problems=len(result["problems"]) if result else None)


async def _verification(user: Any, camera_id: str, start: datetime, end: datetime, include_archived: bool) -> AsyncIterator[dict[str, Any]]:
    """What happens to one verification, as it happens: its place in line, then its progress, then its result. Leaving the iteration (the
    requester went away) gives the place back and stops the check between two files."""
    person = int(user["id"])
    turn = _turns.ask(person)
    cancel = threading.Event()
    work: asyncio.Task[dict[str, Any]] | None = None
    try:
        position = -1
        while not turn.done():
            if (now := _turns.position(turn)) != position:
                position = now
                yield {"queued": position}
            await asyncio.wait({turn}, timeout=1.0)
        progress: dict[str, int] = {"checked": 0, "total": 0}
        work = asyncio.create_task(asyncio.to_thread(_verify_and_record, user, camera_id, start, end, include_archived, cancel, progress))
        shown = None
        while not work.done():
            if dict(progress) != shown:
                shown = dict(progress)
                yield {"progress": shown}
            await asyncio.wait({work}, timeout=PROGRESS_SECONDS)
        yield {"result": work.result()}
    finally:
        cancel.set()
        _turns.leave(person, turn)
        if work is not None and not work.done():
            # Left behind: it stops at the next file, and its outcome (cancelled) is in the security log; nothing else waits for it.
            work.add_done_callback(lambda task: task.cancelled() or task.exception())


def _refuse_a_second(user: Any) -> None:
    if _turns.has(int(user["id"])):
        raise HTTPException(status_code=429, detail="You already have a footage verification running or waiting: let it finish, or cancel it, first",
                            headers={"Retry-After": "5"})


@router.get("/api/cameras/{camera_id}/footage/verify")
@requires_scope("export")
async def verify_footage(camera_id: str, request: Request, user: CurrentUser, start: datetime = Query(), end: datetime = Query()) -> dict[str, object]:
    """Altered, missing or out-of-order segments in [start, end); removals the NVR recorded are expected. Waits for a turn; stops if the
    requester leaves. ``POST .../footage/verify/stream`` says where it stands as it goes."""
    include_archived = _bounds(user, start, end)
    await asyncio.to_thread(_camera_exists, camera_id, user)
    _refuse_a_second(user)
    events = _verification(user, camera_id, start, end, include_archived)
    try:
        while True:
            step = asyncio.ensure_future(anext(events))
            while not step.done():
                await asyncio.wait({step}, timeout=PROGRESS_SECONDS)
                if not step.done() and await request.is_disconnected():
                    step.cancel()
                    await asyncio.gather(step, return_exceptions=True)
                    raise HTTPException(status_code=499, detail="The request was abandoned")
            event = step.result()
            if "result" in event:
                return event["result"]
    finally:
        await events.aclose()


class VerifyRange(BaseModel):
    start: datetime
    end: datetime


@router.post("/api/cameras/{camera_id}/footage/verify/stream")
@requires_scope("export")
async def verify_footage_stream(camera_id: str, payload: VerifyRange, user: CurrentUser) -> StreamingResponse:
    """The verification as it goes, one JSON object a line: ``{"queued": n}`` while it waits (1 is next), ``{"progress": {"checked", "total"}}``
    while it runs, then ``{"result": ...}``. Closing the connection cancels it."""
    include_archived = _bounds(user, payload.start, payload.end)
    await asyncio.to_thread(_camera_exists, camera_id, user)
    _refuse_a_second(user)

    async def lines() -> AsyncIterator[bytes]:
        async for event in _verification(user, camera_id, payload.start, payload.end, include_archived):
            yield (json.dumps(event) + "\n").encode()

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@router.get("/api/footage/signing-key")
def signing_key(_: CurrentUser) -> dict[str, str]:
    """The installation's public key, for checking evidence packages elsewhere."""
    return {"algorithm": "Ed25519", "public_key": footage_chain.public_key()}


class BreakAcknowledgement(BaseModel):
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


@router.get("/api/footage/chain-breaks")
def chain_breaks(_: Administrator) -> dict[str, object]:
    """Every recorded break in a camera's signed footage chain, newest first, with whether it has been acknowledged (#507)."""
    with connect() as db:
        installation_key = footage_chain._key(db)[1]
        rows = db.execute("SELECT b.*,c.name AS camera_name FROM footage_chain_breaks b LEFT JOIN cameras c ON c.id=b.camera_id ORDER BY b.detected_at DESC").fetchall()
        return {"breaks": [{**footage_chain.break_view(row, installation_key), "camera_name": row["camera_name"]} for row in rows]}


@router.post("/api/footage/chain-breaks/{break_id}/acknowledge")
def acknowledge_chain_break(break_id: str, payload: BreakAcknowledgement, user: AdministratorSession) -> dict[str, object]:
    """An administrator, from a browser session (never an API token), says they have seen the break and why it happened: its incident
    recovers (#507)."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = footage_chain.acknowledge_break(db, break_id, int(user["id"]), payload.note)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        if row is None:
            raise HTTPException(status_code=404, detail="No such break")
        return footage_chain.break_view(row, footage_chain._key(db)[1])
