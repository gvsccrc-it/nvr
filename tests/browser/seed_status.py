"""Fill the status panel for its journeys: eleven open incidents (six active, five acknowledged) and eight export jobs (one running, one failed, six finished)."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from tests.browser.seed_fixtures import _camera_id, connect  # noqa: F401 - sets up the import path


def seed() -> dict[str, object]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    camera_id = _camera_id(1)
    with connect() as db:
        for table in ("incidents", "exports"):
            db.execute(f"DELETE FROM {table}")
        for number in range(11):
            state = "active" if number < 6 else "acknowledged"
            when = (now - timedelta(minutes=number * 7)).isoformat()
            db.execute("INSERT INTO incidents(id,incident_key,camera_id,fault_type,summary,state,fault_active,recurrence_count,first_seen_at,last_seen_at,acknowledged_at) VALUES(?,?,?,?,?,?,1,?,?,?,?)",
                       (str(uuid.uuid4()), f"status-{number}", camera_id, "stream_down", f"Fault {number + 1:02d}: stream down", state, 1 + number % 3, when, when, when if state == "acknowledged" else None))
        statuses = ["running", "failed", *["completed"] * 6]
        for number, status in enumerate(statuses):
            created = (now - timedelta(minutes=number * 11)).isoformat()
            start = (now - timedelta(hours=3 + number)).isoformat()
            end = (now - timedelta(hours=3 + number, minutes=-5)).isoformat()
            db.execute("INSERT INTO exports(id,requested_by,camera_ids_json,tile_camera_ids_json,requested_start,requested_end,status,progress,error,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), None, json.dumps([camera_id]), json.dumps([camera_id]), start, end, status, 40 if status == "running" else 100 if status == "completed" else 0,
                        "The encoder stopped" if status == "failed" else None, created, created, (now - timedelta(hours=1)).isoformat() if status == "completed" else None))
    return {"incidents": 11, "active": 6, "acknowledged": 5, "exports": len(statuses)}


def seed_chain_break() -> dict[str, object]:
    """One break in a signed footage chain, with its incident, as the signing pass records it (#507). On a camera id no fixture camera has: a
    break is permanent, and on a fixture camera it would be part of that camera's chain in every later journey (camera-tabs verifies one)."""
    from app import footage_chain

    with connect() as db:
        db.execute("DELETE FROM incidents")
        private, public = footage_chain._key(db)
        break_id = footage_chain._record_break(db, private, public, str(uuid.uuid4()), None, (4, "link 4 no longer matches its signature"),
                                               {"seq": 5, "link_hash": "0" * 64})
    return {"break_id": break_id}


if __name__ == "__main__":
    import sys

    print(json.dumps(seed_chain_break() if "--chain-break" in sys.argv else seed()))
