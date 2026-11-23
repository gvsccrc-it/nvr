# Signed footage

Every recording segment is hashed into a signed, per-camera chain. That makes it possible to show that footage has not been altered, removed or reordered since the NVR wrote it.

## The chain

When a segment is indexed, the background worker reads the file, normally within 5 seconds. It records its SHA-256, time range and size as the next link in that camera's chain. Each link includes the hash of the link before it, so changing, removing or reordering any link breaks every link after it. Hashing happens in the background worker, never on the recorder's write path.

Once a minute, the head of each camera's chain is signed, after checking that the new links extend the signed ones intact (when they do not, see **When the chain no longer matches its signature** below). The head is signed with the installation's Ed25519 signing key. The key is generated on first use and stored in the NVR database, encrypted with the credential key. Configuration backups contain both, so a restored installation keeps its signing key and its old signatures stay valid. The public key is at `GET /api/footage/signing-key`.

Deletions the NVR makes are recorded as links too, so they are expected rather than reported as tampering. This covers retention, administrator footage deletion, and removal of a camera's footage.

Footage recorded before signing was turned on (before this version was installed) is reported as **not signed**, not as tampered.

## How long the chain is kept

The chain does not grow for ever. Once an hour the background worker drops what is past the kept period, a bounded amount at a time:

- **Footage still on disk always keeps its link**, however old it is (footage held for a case, for example), and so does everything recorded after it.
- **Footage the NVR removed keeps its links for 365 days after the removal.** `NVR_FOOTAGE_CHAIN_KEEP_DAYS` changes the period, between 90 days and 10 years (3650).
- **Older links are replaced by a checkpoint**: the newest signed link before which everything is past the period. That link and its signature stay, and the chain that remains verifies from it exactly as it verified from any signature before. The checkpoint is in the `footage_chain_checkpoints` table.
- **Signatures:** every one (one a minute) for the last 30 days, and one an hour for older ones. A link older than 30 days is then checked within an hour's stretch of the chain between two signatures, where a newer one is checked within a minute's.

Verifying a range that begins before the checkpoint answers `before_kept_chain: true` with `kept_from`, the time the kept chain begins. That is information, not a failure: such a range is never reported as tampered for that reason.

An evidence package carries its own extract of the chain, so a package made before its links were dropped still verifies, offline or by upload, for as long as the package is kept.

One thing to know: a segment that vanished without the NVR removing it is reported as **missing** for as long as its link is kept. Once its link is past the period it is dropped like the others, and the range is then simply before the kept chain.

## Verifying a camera's footage

In **Cameras**, open a camera's **Health & History** tab, choose a range under **Verify footage**, and choose **Verify footage**. The API equivalent is:

```http
GET /api/cameras/{camera_id}/footage/verify?start=2026-10-20T00:00:00Z&end=2026-10-21T00:00:00Z
```

Every file in the range is read again, and footage that has moved to a cold tier is downloaded first, so verifying is limited (#508):

- It needs the `export` scope; a `read`-only token cannot verify.
- An administrator may check up to 7 days at a time, archived footage included. Anyone else may check up to 24 hours, of footage on the NVR's own disks: archived footage is reported as `not_checked_archived`, which is not a problem and not a proof either.
- Two verifications run at once, one for each person. The rest wait in line. A person who already has one running or waiting is answered `429`.
- `POST /api/cameras/{camera_id}/footage/verify/stream` with `{"start", "end"}` answers one JSON object a line: `{"queued": n}` while it waits (1 is next), `{"progress": {"checked", "total"}}` while it runs, then `{"result": ...}`, the same answer as the `GET`. The camera's page uses it and shows where it stands, with **Cancel**.
- A verification stops between two files when its requester cancels it or goes away (the `GET` too).
- Each one is in the security log (`footage_verified`: the camera, the range, whether archived footage was read, and the outcome: valid, not valid, cancelled or failed).

Each segment in the range gets one of these statuses:

| Status | Meaning |
| --- | --- |
| `ok` | The file matches its hash and is covered by a valid signature. |
| `ok_unsigned_yet` | The file matches its hash; the link is newer than the last signature (signed within a minute). |
| `not_signed` | Recorded before signing began. |
| `pending` | Not hashed yet. A segment whose file cannot be read is retried every 10 minutes; once it closed more than 10 minutes ago and its file is gone, it is `missing`. |
| `signature_missing` | Matches its hash, but the chain has not been signed in the 10 minutes since: signing is failing or the signatures were removed. |
| `unavailable` | Marked unavailable by the NVR, for example after a restore without its recordings. |
| `not_checked_archived` | On a cold tier, and the person verifying is not an administrator: not read, so neither a problem nor proven. |
| `out_of_order` | Its time range in the index differs from the chain: the footage was moved in time. |
| `missing` | The chain has it but the file or its index entry is gone, without a recorded deletion. |
| `altered` | The file no longer matches its hash. |

The result is `valid` only when no segment is out of order, missing, altered or overdue for a signature, and the chain itself checks out. If the chain has been rewritten, or its signature does not match this installation's key, `chain_error` says so and nothing in it can be trusted.

**What a check reads.** Verifying a range checks the links of the footage in it: each is hashed with its neighbours from the signature before it to the first signature that covers it, and both signatures must be this installation's and valid. The newest signature is checked too. A range is therefore proved against signed points on either side of it without re-reading the camera's whole history, and the check says nothing about footage outside the range asked for.

## When the chain no longer matches its signature

A link that has changed since it was signed can be deliberate editing, damage to the database, or a partial restore. When the minute's check finds one (#507):

- A **break record** is written: the camera, the link that no longer matched, why, when it was noticed, the last good signature and the link signing resumes from. It is signed with the installation's key, and it is permanent: the database refuses to delete or change it, apart from its acknowledgement, which is added once.
- **Signing resumes at once** from the chain's head as it stands, so footage recorded afterwards is signed as before. A second break on the same camera is a record of its own. Before this, the same error was logged every minute and nothing recorded on that camera was signed again.
- An **incident** is raised ("The signed footage of *camera* no longer matches its signature at link *n*"), sent to `system.failure` webhooks and incident emails. It stays active until an administrator acknowledges the break, signed in with a browser (never an API token), with a note saying what happened: in the status panel's incident, or `POST /api/footage/chain-breaks/{id}/acknowledge` `{"note": "..."}`. The acknowledgement is in the security log (`footage_chain_break_acknowledged`). `GET /api/footage/chain-breaks` lists every break, newest first.
- **Verifying a range that spans the break** is not valid: `chain_error` names the break, and `breaks` lists it (with whether its own signature checks out). Footage from where signing resumed verifies as before. An **evidence package** whose chain extract spans a break carries the break record and fails verification with the same message; one made of footage after the break verifies.
- The **nightly self-test** checks each camera's chain from its last signature to its head, so a break is found even if the background worker is not running (see [the self-test](coverage-and-self-test.md)).

## Evidence packages

Before packaging, the export brings the chain up to date and signs it, so footage recorded moments earlier is covered. Each export includes `chain.json` (listed in `integrity.json` like every other member). For each camera, it holds:

- the SHA-256 of every source segment used, taken at export time;
- the chain links from the first of those segments to the **first signature that covers the last of them** (a camera's head is signed about once a minute, so this is the footage's own stretch of the chain, a few links, not everything recorded since); segments recorded before signing began, or hashed but not yet signed when the export was made, are listed as `unsigned_segments` when checked. Packages made before this ran on to the latest signature; they verify the same way;
- the signature and the public key.

`metadata.json` marks each camera `signed` or `not_signed` under `signed_footage`.

The package verifies offline, without the NVR's database. The same check runs on `POST /api/exports/{export_id}/verify`, which also requires the chain to be signed by this installation's key (`signed_by_this_installation`), and fails a package whose `chain.json` was removed, or no longer covers a camera that was signed when it was exported.

What the chain proves, and what it does not:

- It proves the **source recordings** used for the export matched, when exported, what this NVR hashed when it wrote them. That holds only if the package's public key is the installation's public key; compare it with `GET /api/footage/signing-key`. A package can carry any key, so a matching key is what ties it to this NVR.
- The exported MP4s are rendered from those sources (concatenated, time-compressed, masked or overlaid), so they are not byte-identical to them. They are covered only by the unsigned `integrity.json` checksums.
- It does not prove the camera's clock was right.

## What it protects against

The chain detects changes made to the recordings, and to evidence packages, outside the NVR. It does not protect against someone who controls the NVR itself: with the database and the credential key, the signing key can be used to sign anything, and deleting rows from the segment index looks like an NVR deletion. Keep the credential key and configuration backups as secure as the NVR.

The signing key is stored in the database, encrypted with the credential key, rather than as a file beside it. Backups and restores need nothing extra.
