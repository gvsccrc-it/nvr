"""Signed footage: verify a camera's recordings against its signed hash chain."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, StringConstraints

from app import footage_chain
from app.principal import Administrator, AdministratorSession, CurrentUser
from app.store import connect

router = APIRouter()

MAX_VERIFY_WINDOW = timedelta(days=7)
# Verifying re-reads every file in the range, so one runs at a time.
_verifying = threading.Lock()


@router.get("/api/cameras/{camera_id}/footage/verify")
def verify_footage(camera_id: str, _: CurrentUser, start: datetime = Query(), end: datetime = Query()) -> dict[str, object]:
    """Altered, missing or out-of-order segments in [start, end); removals the NVR recorded are expected."""
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(status_code=422, detail="Times must include a timezone")
    if not start < end or end - start > MAX_VERIFY_WINDOW:
        raise HTTPException(status_code=422, detail="Verify up to 7 days at a time")
    with connect() as db:
        if db.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="Camera not found")
    if not _verifying.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Another footage verification is running; try again when it finishes")
    try:
        return footage_chain.verify(camera_id, start, end)
    finally:
        _verifying.release()


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
