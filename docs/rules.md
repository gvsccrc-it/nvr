# Rules

A rule watches cameras for people or vehicles doing one thing, and records a **rule event** when it happens. Rules read the structured detections and tracks the AI worker stores (see [AI detection](ai-detection.md)), so a camera must have AI analysis enabled for its rules to see anything. Administrators manage rules in **Cameras → Rules**; operators do not see the section.

## What a rule contains

- **Cameras.** Individual cameras, and groups: a group includes every camera that uses it as its policy group or appears in it in Live, now and later.
- **Condition**, evaluated on the centre of each object's box:
  - **Enters a zone.** The object is inside the zone. An object that first appears inside it counts as entering.
  - **Crosses a line.** The object crosses the drawn line segment between A and B. Choose *either direction*, *forward* (the direction of the arrow the editor draws) or *reverse*. Crossing the line's extension beyond A or B does not count.
  - **Stays in a zone.** The object stays inside the zone for at least the dwell time. Leaving and coming back starts the time again.
  - **Moves the wrong way.** Inside the zone, the object travels back a tenth of the frame against the expected direction (rightwards, leftwards, downwards or upwards) from the furthest point it reached.
- **Objects**: people, vehicles or both.
- **When** (optional): weekly intervals in a time zone, using the same rules as recording schedules. Outside them the rule does not fire.
- **Quiet time** (`cooldown_seconds`): no two of the rule's events on the same camera are closer together than this. The rules page starts a new rule at 60 seconds; 0 is allowed, and the page marks such a rule "no quiet time". (Through the API it is 0 when left out, as it always was.)
- **Pop alarms into Live** (optional): the rule's alarms bring their camera and its neighbours into Live for people who opted in, and onto video walls (see [alarms in Live and video walls](live-alarms-and-video-wall.md)).

Zones and lines use fractions of the frame and are drawn over a recorded frame from the first camera the rule watches. Every coordinate can be typed. With the keyboard, arrows move a zone or a line end (Shift for larger steps), and Alt+arrows resize a zone.

## One event per object

The rules worker evaluates each object's whole track, so an object produces at most one event per rule, however many frames it meets the condition in. The event is the earliest moment that meets the condition, falls inside the schedule and is not within the cooldown of another event on that camera. It records the rule, camera, track, object class, time and box. Each event raises an [alarm](alarms.md).

A saved rule never fires for moments before it was saved. Editing, enabling or disabling a rule applies to the running worker within a second, without a restart. Upgrading an installation does not evaluate detections stored before the upgrade.

## Actions

Besides raising an alarm, each event can run up to 10 actions. Add them in the rule's **Actions** section:

| Action | What it sends |
|---|---|
| **Signed webhook** | The event as JSON (`event_type` `rule.triggered`), signed exactly like other [webhooks](webhooks.md): `X-NVR-Signature: sha256=…` over `<X-NVR-Timestamp>.<body>` with the action's signing secret (at least 16 characters). |
| **MQTT message** | The same JSON, published at QoS 1 to a topic (optionally retained). The broker is `mqtts://user:password@host:8883`, or `mqtt://` for an authorised private IP address. |
| **Slack message** / **Microsoft Teams message** | An incoming-webhook message: "Gate entry: Vehicle on Loading dock at 10:00:00 UTC", with snapshot and alarm links when `NVR_PUBLIC_URL` is set. The snapshot link expires after 2 hours and names only that alarm. |
| **Camera relay output** | An ONVIF relay pulse on a chosen camera: switched on, held for 0.1 to 60 seconds, then switched off. It uses the camera's first relay unless you name its token. The camera needs an ONVIF device service. |

- **Destinations** follow the webhook rules: public HTTPS (or `mqtts://` for brokers), or a literal private IP address when you tick **Allow a private IP address**. Loopback and link-local addresses are always refused. Destinations are checked when saved and again before each delivery.
- **Secrets** are write-only: webhook, Slack and Teams URLs, broker credentials and signing secrets. They are encrypted with the credential key, like camera credentials, and never returned by the API, written to logs or included in diagnostics bundles. A saved action shows only where it delivers (for example `https://hooks.slack.com` or `mqtts://broker:8883`). Leave a secret empty when editing to keep it.
- **Delivery.** The background worker queues each action once per event and runs deliveries side by side, so a long relay hold never delays a message. Delivery is at least once: a receiver that answers too late may get a retry, so use `event_id` to ignore repeats.
  - **Retries:** connection failures, `429` and `5xx` answers are retried with backoff (2 s, 5 s, 15 s, 1 min, 5 min, 30 min, then hourly; 8 attempts in all).
  - **No retry:** other refusals fail at once, for example a `404`, a broker that rejects the credentials (`mqtt_refused_5`) or a camera with no relay outputs. A relay is always switched off again (retried a few times); one that cannot be is reported as `relay_not_released` and never switched on again by a retry.
  - **Log:** the rule's **Action deliveries** log shows each delivery's status, attempts and last error code for 30 days.
- **Test mode** lists the actions each event would run and runs none of them.

## Test mode

**Test against the last 24 hours** replays the stored detections of the last 24 hours through the rule as it is in the editor, saved or not, and lists every event it would have created. It creates nothing and calls nothing. Time a camera was not analysed cannot match, so "would not have fired" covers analysed frames only.

## API

All rule endpoints require an administrator session, or an API token with the `admin` scope.

```http
GET    /api/rules
POST   /api/rules
GET    /api/rules/{rule_id}
PUT    /api/rules/{rule_id}
DELETE /api/rules/{rule_id}
POST   /api/rules/test
GET    /api/rule-events[?rule_id=<id>][&camera_id=<id>][&limit=200]
```

A rule body:

```json
{
  "name": "Corridor eastbound",
  "enabled": true,
  "camera_ids": ["<camera-id>"],
  "group_ids": [],
  "condition": {"type": "line_crossing", "line": {"x1": 0.5, "y1": 0.1, "x2": 0.45, "y2": 0.9}, "direction": "forward"},
  "classes": ["person"],
  "schedule": {"timezone": "Europe/London", "intervals": [{"days": [0, 1, 2, 3, 4], "start": "18:00", "end": "07:00"}]},
  "cooldown_seconds": 60,
  "pop_into_live": false,
  "actions": [
    {"type": "slack", "name": "Slack #security", "url": "https://hooks.slack.com/services/…"},
    {"type": "mqtt", "name": "Building bus", "url": "mqtts://nvr:…@broker.example:8883", "topic": "site/alarms"},
    {"type": "relay", "name": "Gate siren", "camera_id": "<camera-id>", "duration_seconds": 2}
  ]
}
```

Actions are returned with an `id` and a `target`, never their `url` or `signing_secret`. Send an action back with its `id` to keep its saved secrets, and leave an action out to remove it. `GET /api/rules/{id}/deliveries` lists the rule's action deliveries, newest first.

The other conditions are `{"type": "zone_entry", "zone": {"x", "y", "width", "height"}}`, `{"type": "loitering", "zone": {...}, "dwell_seconds": 30}` `{"type": "inactivity", "zone": {...} or null, "still_seconds": 300}` and `{"type": "wrong_way", "zone": {...}, "direction": "right"}`. An inactivity rule fires once when an object's centre has stayed within 3% of the frame of where it stopped for `still_seconds` (10 to 86400), anywhere in the frame or only inside the zone; the clock restarts when the object moves, and the rule can fire again after that. It uses the tracks the AI worker already stores, so it adds no model and no load.

An occupancy rule is `{"type": "occupancy", "zone": {...} or null, "limit": 5, "for_seconds": 30}`: it fires when more than `limit` of the rule's classes are in view (or inside the zone) for the whole of `for_seconds`. An object counts if the AI detected it within 2 seconds of the moment, because the AI does not look at every frame. It looks at several tracks at once, so it is worked out from the camera's stored detections rather than one track. **One over-limit stretch is one alarm**, however long it lasts, live as in test mode: the rules worker keeps a running count for each rule and camera (fed only what was stored since its last pass) and remembers that the stretch it is in has been reported. When the count drops back, or the camera has had no detection at all for 10 seconds, the alarm's history records when the stretch ended, and the next stretch is a new alarm (not within a minute of the last, or the rule's cooldown if longer). Add `"remind_minutes": 30` (15 to 1440) to be reminded while a stretch goes on: each reminder is a note in the alarm's history and a notification to the devices that want the alarm, never a new alarm, and there are none for an alarm that has been resolved. Reminders are off unless set. After the rules worker restarts, a stretch it had reported and that is still going is carried on, not reported again. Test mode replays it over the stored detections. Creating, changing and deleting a rule is recorded in the audit log. Deleting a rule keeps its past events, which still carry the rule's name.

## Limits on what a rule can raise

- **A ceiling for every rule.** One rule raises at most 10 alarms a minute and 60 an hour on one camera (`alarms.CEILING_A_MINUTE`, `CEILING_AN_HOUR`). Past that its events are still recorded (test mode, Insights and the events list see them), but no alarm is raised and nothing is sent: no push, webhook, Slack, Teams or MQTT message, no relay pulse. One summary alarm says at once that further alarms are being held back, and another every 10 minutes while it lasts says how many were. When the rule is under its ceiling again, alarms flow as before, after one last summary of what was held back. A summary reaches the inbox, push devices and webhooks, and runs none of the rule's actions. **A fall alarm is never held back.**
- **A reconnect does not raise everything again.** When a camera's stream reopens (or the AI worker restarts), the objects it was following within the last 30 seconds are taken up again where they were, each with its track, so a rule that has fired for an object does not fire for it again. An object that appears somewhere else, or more than 30 seconds later, is a new object.
- **What is found long after it happened tells no one.** If the rules worker was down or behind, an event more than 10 minutes older than the newest detection stored is recorded and its alarm is in the inbox, marked as found late, but nothing pops into Live and nothing is sent.

## Reliability

Rules run in the supervised `rules` worker process (see [architecture](architecture.md)). If it stops, a `worker_rules` incident is raised and the worker is restarted. Detections stored while it was down are evaluated when it is back, because the worker keeps its place by detection rather than by time.
