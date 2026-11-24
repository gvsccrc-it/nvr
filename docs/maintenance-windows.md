# Maintenance windows (#234)

While cameras are being worked on, they should not set off alerts. A **maintenance window** says "this camera, these groups, or the whole site is being worked on from now until then".

## Using it

- **Cameras > Maintenance**: give what is being done, choose the cameras (administrators can also choose camera groups or the whole site), how long, and optionally a later start. **Start maintenance.**
- **On a camera** (Cameras > select a camera): **Start maintenance (1 hour)** with one press, and **End maintenance** to stop early. A camera in a window shows **In maintenance**.
- Windows are one-off: a start and an end. Operators can open a window on cameras they can see for up to **8 hours** (starting within 24 hours); administrators can cover cameras, groups or the whole site for up to **7 days**.
- **Extending**: a window that has not ended can be made longer with **Extend (its name) by 30 minutes** or **by 1 hour** (in the status bar's maintenance list and on Cameras > Maintenance), by whoever may end it (an administrator any window, anyone else the windows they opened), up to the same limit as opening one: 8 hours in all for operators, 7 days for administrators. Past that, end the window and start another. Extending is audited (`maintenance_extended`).
- **In every workspace** the status bar shows **Maintenance: 3 cameras** (or **Maintenance: whole site**) while a window covers cameras you can see. Pressing it lists the windows with what each has held back, an **End now** button for the ones you may end (an administrator any window, anyone else the windows they opened), and a link to start or manage them.
- A window can be ended early, or, before it has started, cancelled. Everything is audited (`maintenance_opened`, `maintenance_ended`).

## What happens in a window

- **Alarms from rules are kept but silent.** The alarm is created, appears in the inbox marked "during maintenance (name)", and keeps its clip, but sends no push, no Live pop-in, no webhook and runs none of the rule's actions (email, Slack, relay...).
- **Incidents** (camera offline, recording failure, tamper, AI worker, disk...) raise no webhook. They still show in the incident list and on the camera. A fault that belongs to no camera (a worker, the disk) is quieted only by a window for the whole site.
- **Recording carries on.** Footage is the point of the NVR and is never paused.
- **AI analysis of the covered cameras pauses** for as long as the window covers them: the AI worker stops reading and analysing their pictures within about 5 seconds of the window starting, and starts again within about 5 seconds of it ending (or being ended early). A paused camera does not take one of the AI's places, so another camera waiting for room can be analysed meanwhile. The camera's AI status reads **maintenance** ("Paused for maintenance: name"). The paused time is recorded as **not analysed**, with that reason: Insights marks those hours "not fully analyzed", search's coverage and the playback timeline list it as not analysed ("Paused for maintenance: name"), so nobody reads an empty stretch as "nobody was there". Cameras outside the window are not affected. Privacy masks are unchanged.
- **When the window ends**, a fault that is still there is announced then, on the next check (within a minute or so), so a camera left broken does not stay silent. A fault that was mended during the work is never announced, and no recovery notice is sent for something that was never announced.
- **A reminder before it ends.** The person who opened a window is reminded shortly before it ends, so they can extend it if the work is not finished: **10 minutes** before the end (for a window shorter than 40 minutes, a quarter of its length before: 5 minutes for a 20-minute window; none for a window shorter than 4 minutes). The reminder is a push notification to the devices they turned notifications on for (whatever their alarm choices and quiet hours: they are working in that window), which opens Cameras > Maintenance; and in the interface the status bar's maintenance chip says **(ends soon)** and the window shows **Ends soon** with the extend buttons. It is sent once a window; extending the window sets it again for the new end (unless the new end is already that close, when the person extending it has just seen it). Nothing is sent for a window that was ended early or cancelled, or whose end has passed.
- Each window counts what it held back (alarms, incident notifications), shown in the list and recorded when it ends, so a window that hid something real is visible afterwards.

## Not built

Recurring windows and quieting digests (a digest still lists what happened): the owner chose not to have them for now (24 November 2026). The AI pause applies to every window; there is no per-window choice.
