# Recording health and retention

Camera status distinguishes the stream connection from persisted footage. The camera inventory returns video freshness, last indexed footage write, and a coverage state. Coverage history records transitions as covered, expected idle, known failure, or uncertain. Motion-only idle is expected when the ONVIF event source and video stream remain healthy; a stale continuous write while video is fresh is reported as a recording failure. Each interval also records why footage is missing; see [coverage and self-test](coverage-and-self-test.md).

Retention defaults to 30 days. An administrator may override the target for a camera group or an individual camera. A camera override wins over its group; removing it restores inheritance. The authenticated camera retention endpoint reports the effective target, achieved time span, indexed bytes, free filesystem capacity, and a projection based on observed indexed bitrate. The projection is an estimate and does not promise a fixed retention duration.

Cleanup runs periodically and removes catalog entries only after their files have been removed or were already missing. It retains an active playback lease until the response completes, so cleanup cannot delete footage being served. Failed filesystem deletions leave the catalog record for retry.

The retention API is:

- `GET/PUT/DELETE /api/camera-groups/{id}/retention`
- `GET/PUT/DELETE /api/cameras/{id}/retention`
- `GET /api/cameras/{id}/coverage`

Retention targets are integers from 1 through 3650 days. Operators can read effective policy and achieved coverage; only administrators can change retention targets.

**Changed settings reach a running recording.** Switching audio off, changing a camera's address or login, its motion settings, or its group's storage volume restarts that camera's recording within a few seconds, so the new setting is used at once and not only after the stream next drops. The few seconds without footage show in coverage as "Restarted for changed settings". A change that touches many cameras (a group's volume) restarts them four at a time. Saving a schedule that leaves a camera's mode as it was restarts nothing.

**After the recorder was stopped abruptly.** If the recording process is killed (a restart that ran out of time, the machine short of memory), the segment each camera was writing is left unfinished and the last finished ones may not be in the index. Within five minutes of the next start, and every five minutes after, the recorder goes over the folders of earlier runs: finished segments are added to the index, so they play and expire like any other footage, and the unfinished file, which cannot be played, is removed once its folder has been untouched for five minutes. The log says what it did.

## Multiple recording volumes

The default Compose file provides a separate `archive` volume mounted at `/recordings/archive`. Set `NVR_STORAGE_VOLUMES` to a JSON object that maps volume names to directories below `NVR_RECORDINGS_PATH`, for example `{"default":"","archive":"archive"}`. Every non-default directory must be a mounted filesystem; the recorder reports it unavailable when the mount is missing. Add additional Compose bind or named volumes at matching paths to expose more local disks.

Administrators can inspect `/api/storage` and assign a group through `GET/PUT /api/camera-groups/{id}/storage-volume`. Storage status includes mount, write-probe latency, capacity, free space, and affected cameras. Low free space and unavailable or slow volumes report affected cameras and retention risk.

## A recordings disk that is not mounted

When the host disk behind a recordings folder fails to mount (after a reboot, a loose cable, a failed disk), the folder is still there, but empty, on the disk underneath it, which is usually the small system disk. Left alone, the NVR would record onto that disk until it filled and then "remove" its oldest footage to make room, forgetting footage that is safe on the disk that is not there.

So every volume, the default one included, carries a small marker file at its root (`.nvr-volume.json`). Do not delete or copy it.

- **The marker is there:** the volume is used as normal.
- **No marker, and the NVR has no footage on the volume:** a first use. The marker is written.
- **No marker, but recorded files are there:** an installation from before markers, or a marker deleted by hand. The footage is the proof and the marker is written. Nothing needs doing on upgrade.
- **No marker and none of the recorded files:** the disk is not mounted. The volume shows as **disk not mounted**, an incident is raised, nothing is recorded to it, and none of its footage is removed by retention, headroom or tiering. **Mount the disk**; recording carries on by itself within a few seconds.
- **A marker from another NVR:** the volume shows as **another NVR's disk** and is treated the same way.

**If you replaced the disk on purpose,** open **Settings › Recording & storage › Volumes** in a browser as an administrator and choose **This is a new, empty disk** for that volume. The NVR writes a marker on the new disk and forgets the footage that was on the old one (it can no longer be played or exported from this NVR). The action is recorded in the security log (`storage_disk_replaced`) and cannot be done with an API token.

After a configuration **restore**, the disks mounted at that moment are taken as the installation's own, and footage they do not hold is marked unavailable, as before.

## Storage headroom

Storage space comes before the retention policy. A recorder fills its disks by design, so each storage volume keeps a **headroom** of free space. When free space falls below it, the **oldest footage on that volume is deleted, across all its cameras and even if it is still inside its retention policy**, until the headroom is free again. This keeps recording running and leaves room for exports to be written, instead of a full disk stopping recording and failing exports with "No space left on device".

- The headroom is set per volume under **Settings > Recording & storage > Storage headroom**, in GB (`PUT /api/storage/{volume_id}/headroom`, administrators only, audited as `storage_headroom_changed`; `null` restores the default). The default is 5% of the volume, and at least 5 GB.
- It is checked every minute. Footage that is held, being exported (queued or running), kept for an open or recently resolved alarm, or being played is never deleted; if those alone fill the volume the setting shows how far short of the headroom it is. Footage already moved to a cold tier is not on the volume, so it is not counted.
- Beside each setting the page shows the headroom and the free space now, whether footage is being deleted to keep it, how much was deleted in the last 24 hours, the age of the oldest footage kept against the longest retention policy on the volume, and any shortfall.
- Deletions are counted in the day's retention deletion record like any other, so the coverage and playback show the footage as removed.
- Footage removed this way can be younger than its retention policy: the retention forecast shows which cameras cannot be held for their policy at their write rate.

## Sections of Settings › Recording & storage

The page shows one section at a time, chosen from the tabs along its top: Volumes, Headroom, Disk health, Forecast, Retention (the default and the alarm footage period) and Backups. The tabs work with the arrow keys. The page opens on Volumes each time.

**Reconnects are spread out (#495).** A camera whose stream drops is tried again after 2 to 3 seconds, then after waits that double up to 30 seconds, each with a random share (from half to one and a half times the step, the same on average). Cameras that dropped together, after a switch rebooted or the power went, no longer all start FFmpeg, test their connection and write their status in the same second, and do not all reconnect in the same second when the network comes back: with 200 cameras, at most about 60 attempts fall in any one second, and about 20 once the waits reach their longest. At start the recorder starts cameras 16 at a time, half a second apart.
