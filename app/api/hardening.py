"""Camera network hardening: the report, the camera networks, running the checks, and acknowledging findings."""
from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import hardening
from app.audit import record
from app.principal import Administrator
from app.store import connect

router = APIRouter()
logger = logging.getLogger(__name__)
# How long a request that starts the checks waits for them (#477). Most runs finish well within it (a camera asked within the hour is not
# asked again) and the answer is the result, as before. A run with cameras that do not answer goes on in the background, and the answer
# says how far it has got: the page asks again until it is done. It used to hold the request for minutes.
RUN_WAIT_SECONDS = 3.0


async def _start_run(admin_id: int) -> dict[str, object]:
    """Start the checks in a thread of their own, wait up to RUN_WAIT_SECONDS, and answer with the report as it stands then."""
    finished = threading.Event()

    def work() -> None:
        try:
            result = hardening.run("manual")
            if result.get("ran"):
                with connect() as db:
                    record(db, admin_id, "hardening_run", "hardening", "checks", high=result["counts"]["high"], medium=result["counts"]["medium"])
        except Exception:  # noqa: BLE001 - the page shows the run as no longer going; the error is in the log
            logger.exception("The hardening checks failed")
        finally:
            finished.set()

    threading.Thread(target=work, name="hardening-run", daemon=True).start()
    await asyncio.to_thread(finished.wait, RUN_WAIT_SECONDS)

    def now_report() -> dict[str, object]:
        with connect() as db:
            return hardening.report(db)

    return await asyncio.to_thread(now_report)


class Settings(BaseModel):
    camera_networks: list[str] = Field(max_length=hardening.MAX_NETWORKS)
    probes_enabled: bool | None = None  # Left out, the setting is left as it is.


class Acknowledgement(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


def _known(check: str) -> None:
    if check not in hardening.CHECKS:
        raise HTTPException(status_code=404, detail="Finding not found")


@router.get("/api/hardening")
def get_report(_: Administrator) -> dict[str, object]:
    with connect() as db:
        return hardening.report(db)


@router.post("/api/hardening/run")
async def run_checks(admin: Administrator) -> dict[str, object]:
    """Run every check now. A camera probed within the probe gap is not probed again: its last answer is used. Answers within a few
    seconds: with the result when the run is done by then, otherwise with its progress (``run``) while it goes on in the background."""
    return await _start_run(admin.id)


@router.put("/api/hardening/settings")
async def put_settings(payload: Settings, admin: Administrator) -> dict[str, object]:
    try:
        hardening.parse_networks(payload.camera_networks)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    def save() -> None:
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                before = [str(network) for network in hardening.networks(db)]
                after = hardening.set_networks(db, payload.camera_networks)
                record(db, admin.id, "hardening_networks_set", "hardening", "networks", before=before, after=after)
                if payload.probes_enabled is not None and payload.probes_enabled != hardening.probes_enabled(db):
                    hardening.set_probes_enabled(db, payload.probes_enabled)
                    record(db, admin.id, "hardening_probes_set", "hardening", "probes", enabled=payload.probes_enabled)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    await asyncio.to_thread(save)  # Off the event loop (#354)
    return await _start_run(admin.id)  # Applied at once when it can be; never held up for minutes by cameras that do not answer (#477)


@router.put("/api/hardening/findings/{camera_id}/{check}/acknowledgement")
def acknowledge(camera_id: str, check: str, payload: Acknowledgement, admin: Administrator) -> dict[str, object]:
    _known(check)
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="Say why this finding is accepted")
    with connect() as db:
        if not hardening.acknowledge(db, camera_id, check, reason, admin.id):
            raise HTTPException(status_code=404, detail="Finding not found")
        record(db, admin.id, "hardening_finding_acknowledged", "camera", camera_id, check=check, reason=reason)
        return hardening.report(db)


@router.delete("/api/hardening/findings/{camera_id}/{check}/acknowledgement")
def restore(camera_id: str, check: str, admin: Administrator) -> dict[str, object]:
    _known(check)
    with connect() as db:
        if not hardening.restore(db, camera_id, check):
            raise HTTPException(status_code=404, detail="No acknowledged finding")
        record(db, admin.id, "hardening_finding_restored", "camera", camera_id, check=check)
        return hardening.report(db)
