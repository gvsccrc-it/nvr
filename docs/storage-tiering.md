# Storage tiering to a cold tier or S3-compatible storage

Footage older than an age you choose moves from the recorder's disks to a **cold tier**: a folder (another disk, a network share mounted into the container) or an S3-compatible bucket (AWS S3, MinIO, Ceph, Backblaze B2, Wasabi...). It stays in the timeline and plays back through this NVR; holds, exports, signed-footage verification and retention work across tiers.

## Setting it up

Settings, **Storage tiering**:

1. **Add a cold tier.** A *folder* is an absolute path outside the recordings folder, writable by the NVR. A *bucket* is the store's address, the bucket, a region (default `us-east-1`), an optional prefix inside the bucket, and an access key and secret key. The NVR writes a small object, reads it and removes it before it keeps the tier, so a tier that does not work is refused with the reason. Keys are stored encrypted like camera credentials and are never shown again; a public store must use HTTPS, and one on a private address needs the private-network box ticked.
2. **Set a policy for a storage volume** (the default volume, or any volume in `NVR_STORAGE_VOLUMES`): the tier, the age in days after which footage moves, and a rate limit in **MiB a second** (1 MiB = 1,048,576 bytes; the API's `rate_kbps` holds the same limit in KiB, 1,024 bytes, a second: the default 20,480 is 20 MiB a second). The limit covers everything a policy's moves send and read back, across every segment, not each one separately. One policy per volume; it applies to every camera whose group uses that volume.

The age is separate from retention: retention still deletes footage when its policy says (from whichever tier it is on, with its copy). Set the move age below the retention days or nothing will ever reach the tier.

## How a move goes

1. Pick the oldest eligible segments (older than the age, not being recorded, not already moved), 20 at a time, and move them one at a time, rate limited. While footage is waiting, batch follows batch and pass follows pass at once; only when nothing is left does the mover wait 30 s before looking again. (It used to move 20 and then sleep 30 s: at most 40 segments a minute, while 200 cameras record 200, so above about 40 cameras on one volume the backlog only grew.) A tier that is failing ends the pass after one batch, and the next pass tries again.
2. **Skip** a segment that is **held** or is in a **queued or running export**; both stay hot until released. They are left out when the pass picks its segments, so any number of them at the front never stops the footage behind them from moving. A segment whose **file is missing** from its volume is remembered, looked for again an hour later, and counted in the tier's status (`missing_files`); it does not hold up the others either. A segment whose footage no longer matches its signed chain is **never moved** (it is shown in the tier's last error and left alone).
3. Hash the hot file, **copy** it to the tier (to a partial file or a signed upload), and **confirm the copy** before anything else happens (see below).
4. Check again that the segment is not held or exported, then switch its row to the tier. It keeps its id and path, which is what the signed chain, holds, exports and cases refer to.
5. Delete the hot file, unless someone is playing it; a later pass removes it once it is released.

Every step can be repeated: the copy overwrites, the switch only happens once, and a pass first finishes anything interrupted. Whatever the interruption (the copy dies half way, the process stops after the switch before the hot file goes), the segment stays readable once, and no duplicate or partial copy is left behind. A tier that fails a few times in a row raises a storage incident, which recovers with its first success.

## How a copy is confirmed

The hot copy of a segment is deleted only after the tier has confirmed it holds the same bytes. That rule does not change; how the tier confirms it depends on the tier. This is the owner's decision on #486:

- **A folder tier** is read back in full and its SHA-256 and size checked: it costs only disk time.
- **A bucket**: the upload is signed with the file's SHA-256, so the store itself refuses a body that does not match, and the same digest goes as the object's checksum (`x-amz-checksum-sha256`). The NVR then asks for the object's details (a `HEAD` request with `x-amz-checksum-mode: ENABLED`) and checks the size and the checksum the store kept. Nothing is downloaded: reading every file back was a billed download per segment, and took a third of the move's time under the rate limit.
- **A bucket whose store keeps no checksums** (it does not give one back) is read back in full, as before. The tiering page says so on the tier ("this store keeps no checksums, so every copy is read back"); whether a store keeps them is found out by the first move, and again after the tier's settings change.
- **"Read every file back after upload"**, a per-tier switch on the page (`PUT /api/admin/tiering/tiers/<id>/read-back`), has a bucket read back in full anyway, for an owner who wants that whatever it costs.

Either way, a copy that does not match is never trusted: the move fails, the hot copy stays, and the next pass sends it again.

## When tiering falls behind

The page shows, for each volume, how many segments are **waiting to move** (due by their age, not held: held footage never moves) and when the **oldest** of them was recorded. It is measured every 10 minutes and the last hour of measurements is kept.

If the backlog **keeps growing for an hour** (no measurement smaller than the one before, the last larger than the first) while its oldest footage is more than a day past the move age, a storage incident is raised: "Tiering on volume … is falling behind". Footage that cannot move in time is otherwise deleted to keep disk headroom instead of being archived, with no other warning. A backlog that rises and falls between passes is keeping up and raises nothing. The incident recovers once the backlog stops growing, or when the policy is turned off or its disk is away. What to do: raise the policy's rate limit, check the tier (a failing tier raises its own incident), or move footage sooner.

Finding the oldest waiting footage reads the index `recording_segments_tier_due_idx` (migration 105) in order and stops at the batch: it no longer scans and sorts the whole segment table every pass (measured on a million segments: 334 ms before, 0.1 ms after).

## Playing and verifying

- A segment on a **folder** tier is read in place. A segment in a **bucket** is fetched into a read-through cache (`NVR_TIER_CACHE_PATH`, default beside the database; `NVR_TIER_CACHE_BYTES`, default 2 GiB, least recently used out, never footage being played) and checked against the digest it moved with before it is used; if it came back different it is refused. The first seconds of footage from a bucket are slower.
- Playback, exports, snapshots, case clips and the footage chain's verification all read through the same path, so they work unchanged. Verification of a long range on a bucket downloads every segment in it.
- The timeline marks footage on a tier in its tooltip.

## Deleting

Retention, deleting a segment and deleting a camera's footage remove the cold copy and the cached copy too. A bucket that cannot be reached for the delete does not stop retention: the object is remembered and removed on a later pass.

## Limits

- A tier's location cannot be changed, or the tier removed, while footage is on it.
- Moving needs the recorder's hot copy to be readable; footage already unavailable is not moved.
- Playing footage from an object store is cache-then-serve, not streaming: the segment is fetched whole into the read-through cache (verified against its hash) and then served, so the first play of a cold segment waits for the download.
- A configuration backup does not know about footage moved after it was taken: restoring an older backup loses track of what moved (the objects stay in the bucket).
- A bad copy found when confirming is not deleted (its name is shared with any other mover); it is overwritten on the next pass.
- Cold storage is not a backup: it holds the footage that left the disks, one copy, with whatever durability the store gives.
- Tested here with a local folder and with a stand-in S3 server that checks AWS Signature Version 4 and upload hashes, and with the signing code checked against examples AWS publishes; the CI workflow also runs the object-store tests against a separate S3 server (moto's, with signing checked). Try your own store (see the acceptance checklist) before relying on it.

## API

Administrators: `GET /api/admin/tiering`; `POST /api/admin/tiering/tiers`, `PUT`/`DELETE …/tiers/<id>`, `POST …/tiers/<id>/check`, `PUT …/tiers/<id>/read-back` (`{"read_back": true}`); `PUT`/`DELETE /api/admin/tiering/policies/<volume>`; `POST /api/admin/tiering/run` (moves up to five of each policy's oldest segments now). Changes are in the audit log (`cold_tier_created`, `cold_tier_updated`, `cold_tier_deleted`, `tier_policy_changed`, `tier_policy_removed`, `cold_tier_checked`, `cold_tier_read_back_changed`, `tiering_run_now`). The view gives each tier's `confirmed_by` (`read_back`, `read_back_chosen`, `read_back_no_checksums`, `checksum`), `read_back` and `checksums`, and `backlog` per volume (`segments`, `oldest_ends_at`, `measured_at`, `growing`). Timeline segments carry `archived`.
