"""Browser push subscriptions are limited to the browsers' own push services (#523).

A subscription made before this to any other address is removed: the person switches notifications on again in that browser. The list is
written out here, as it was when this step was made; ``NVR_PUSH_EXTRA_HOSTS`` is honoured, as it is afterwards.
"""
from __future__ import annotations

import json
import os
import sqlite3
from urllib.parse import urlsplit

NAME = "push_services"

_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com")
_SUFFIXES = (".push.apple.com", ".notify.windows.com")


def _allowed(endpoint: str) -> bool:
    try:
        host = (urlsplit(endpoint).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    extra = {item.strip().lower() for item in os.environ.get("NVR_PUSH_EXTRA_HOSTS", "").split(",") if item.strip()}
    return endpoint.startswith("https://") and bool(host) and (host in _HOSTS or host in extra or host.endswith(_SUFFIXES))


def upgrade(db: sqlite3.Connection) -> None:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "push_subscriptions" not in tables:
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(push_subscriptions)")}
    web = "kind='web'" if "kind" in columns else "1"
    for row in db.execute(f"SELECT id,user_id,endpoint,device_label FROM push_subscriptions WHERE {web}").fetchall():
        if _allowed(str(row[2])):
            continue
        db.execute("DELETE FROM push_subscriptions WHERE id=?", (row[0],))
        if "audit_log" in tables:
            db.execute("INSERT INTO audit_log(actor_id,action,resource_type,resource_id,details_json) VALUES(NULL,'push_unsubscribed','push_subscription',?,?)",
                       (str(row[0]), json.dumps({"device": row[3], "reason": "not_a_browser_push_service", "user_id": row[1]}, sort_keys=True)))
