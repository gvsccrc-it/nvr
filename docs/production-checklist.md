# Production checklist

Go through this list before the NVR records anything that matters, and again after each upgrade. Each item says what to do and how. Most of them have caused an outage or an exposure somewhere, or would. The detail lives in the pages linked; this page puts it in one place (#553).

Addresses below are examples from the documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`) and `example.test`: use your own.

## 1. Network

- [ ] **Close the NVR's port to the camera network.** The NVR uses the host's network directly (`network_mode: host`), so its web port (8280, or `NVR_PORT`) is open on every interface of the host, including the one on the camera network. Allow it only from the networks people use. With `ufw`, for an office network `192.0.2.0/24`:

  ```sh
  sudo ufw default deny incoming
  sudo ufw allow from 192.0.2.0/24 to any port 8280 proto tcp
  sudo ufw allow from 192.0.2.0/24 to any port 22 proto tcp   # keep your own way in
  sudo ufw enable
  ```

  ONVIF event delivery from the cameras also reaches the NVR on this port: allow the camera network to it only if you use ONVIF events (`sudo ufw allow from 198.51.100.0/24 to any port 8280 proto tcp`).
- [ ] **Keep the cameras on their own network or VLAN**, reachable by the NVR and nothing people use. Some cameras ask for their password in a form anyone on their network can read (Basic sign-in), and the NVR's recording answers it (#476); see [camera-onboarding.md](camera-onboarding.md#a-camera-that-asks-for-its-password-unprotected).
- [ ] **HTTPS before real use.** Put the NVR behind a reverse proxy or one of the tunnels in [remote-access.md](remote-access.md) (Tailscale, Cloudflare Tunnel, WireGuard). Plain HTTP sends passwords and video unencrypted.
- [ ] **Tell the NVR how it is reached.** In `.env`:
  - `NVR_PUBLIC_URL=https://nvr.example.test` (the address people use);
  - `NVR_TRUSTED_PROXIES` (the proxy's address; the tunnel overlays set `127.0.0.1`);
  - `NVR_COOKIE_SECURE=true` (it is on by itself when `NVR_PUBLIC_URL` is `https://`).

  With a proxy or tunnel on the same host, also set `NVR_LISTEN_HOST=127.0.0.1` so nothing else reaches the NVR directly. Settings › Remote access warns about what is still missing. See [remote-access.md](remote-access.md).
- [ ] **Check which camera addresses are allowed.** The NVR connects only to private camera addresses unless told otherwise ([camera onboarding](camera-onboarding.md#which-camera-addresses-the-nvr-connects-to)). After the upgrade that brought this in, open Settings › Camera hardening: cameras listed under "Cameras at public addresses" keep working, and need "This camera is at a public address" ticked the next time their source is changed. Set `NVR_CAMERA_ALLOW_LOOPBACK=true` only if cameras reach the NVR through a restreamer on the same machine (addresses `127.0.0.1` or `::1`).

## 2. First run

- [ ] **Create the administrator before the host joins any other network.** Until the first administrator exists, anyone who can reach the page and has the setup code can become it. The code is in the log: `docker compose logs nvr | grep "setup code"`. Do it on the setup network. The setup page closes for good once the administrator exists.
- [ ] **Turn on a second factor** for administrators (the account menu, **Security…**). See [second-factors.md](second-factors.md).

## 3. The image

- [ ] **Run a released version, never `edge`.** `compose.yaml` names one (`image: ghcr.io/gvsccrc-it/nvr:X.Y.Z`). `edge` is the latest merge to `main`, for a test machine only (#540). To pin harder, use the digest: `image: ghcr.io/gvsccrc-it/nvr:X.Y.Z@sha256:…`.
- [ ] **Check the image's signature before running it** (`cosign verify`, the command in [guided-updates.md](guided-updates.md), "Checking an image before you run it"). The Updates page's steps name a release by its digest and show the command.
- [ ] **Write down the image in use before every upgrade**: `docker compose images nvr`. It is the way back. Keep it with the upgrade notes, off the machine.

## 4. Backups

Configuration backups (database and credential key, never footage) are **off until a passphrase is set**. See the README, "Configuration backups and restore".

- [ ] **Turn them on.** Set `NVR_BACKUP_PASSPHRASE` (at least 16 characters), or better, `NVR_BACKUP_PASSPHRASE_FILE`. Keep the passphrase somewhere other than the NVR's host: without it a backup cannot be opened.
- [ ] **Write them to another disk.** Replace the `nvr-backups` volume with a folder on a different disk or host: `- /mnt/offsite/nvr-backups:/backups`. The NVR runs as user 10001 with no privileges, so give it the folder first:

  ```sh
  sudo chown -R 10001:10001 /mnt/offsite/nvr-backups
  ```

  The same applies to any recordings disk mounted from the host. The NVR raises an incident at start if it cannot write to a folder it was given.
- [ ] **Copy them off the machine** (another site, or cloud storage), on a schedule of your own.
- [ ] **Try a restore** on a test machine before you need one, and after each major upgrade.
- [ ] **Keep the credential key with the data.** Camera passwords are encrypted with `/secrets/credentials.key`. A database without its key cannot open them. See [database-recovery.md](database-recovery.md).

## 5. Clocks

- [ ] **Keep the host's clock synchronised** (NTP: `chrony` or `systemd-timesyncd`). Check with `timedatectl`: "System clock synchronized: yes". Everything below depends on it:
  - authenticator codes (a few seconds out and they are refused);
  - the times on signed footage and in the footage chain;
  - session expiry;
  - recording timestamps, and so search and playback;
  - the 60-second window between a failover pair ([failover-recorder.md](failover-recorder.md)).
- [ ] **A failover pair talks over its HTTPS link**: each NVR's address is the other's `https://<address>:8443` with its certificate fingerprint, port 8443 is open between the two NVRs only, and the hardening page shows no *Unencrypted failover link* ([failover-recorder.md](failover-recorder.md)).
- [ ] **Keep the cameras' clocks right too**: point each camera at the same time server, in the camera's own settings. A camera's clock is what it stamps on its picture and its ONVIF events.

## 6. Disks

- [ ] **Watch the system disk as well as the recordings disk.** The database, the container's own files and the logs live there. A full system disk stops the database, and with it everything.
- [ ] **Container logs are bounded**: `compose.yaml` keeps at most 100 MB (`logging`, #543). Keep that if you change the file.
- [ ] **The motion buffer is on the system disk.** By default it is `/tmp/nvr-motion-buffer` in the container's own layer (`NVR_MOTION_BUFFER_PATH`). Where it should be by default is an open question for the owner (#339). Until then, leave room for it on the system disk, or point it at a folder on the recordings disk.
- [ ] **Keep headroom on the disk that holds the database.** An upgrade that changes the database first copies it (`nvr.sqlite3.pre-migration-*`, beside it). Only three copies are kept, and an older one is removed to make room when the disk is nearly full.
- [ ] **Copy a good pre-migration copy off the machine after each upgrade** that changed the database. It is the way back if the upgrade goes wrong. See [guided-updates.md](guided-updates.md).
- [ ] **Turn on disk health** (SMART) for the recording disks. It is commented out in `compose.yaml`; see [disk-health-and-retention-forecast.md](disk-health-and-retention-forecast.md).

## 7. Memory

`mem_limit` is commented out in `compose.yaml`. Choose deliberately:

- **Without a limit**, when the host runs out of memory the kernel chooses what to kill, anywhere on the host. That can be the recorder, the supervisor, or something else entirely.
- **With a limit**, the largest process inside the container is killed, usually the AI worker, and the supervisor restarts it. Recording carries on.

**Set a limit.** Size it from what the NVR really uses: run the site at its normal load (every camera recording, a few live views, an export, AI on as you will use it) for a day, read the peak with `docker stats nvr`, and set `mem_limit` to about one and a half times that. As a first figure only, before you have measured: about 1 GiB, plus 150 MiB a camera, plus 1 GiB for each export running at once (one by default), and more with AI on many cameras. No better figure has been measured yet.

## 8. Hardening

What `compose.yaml` sets, and what it does not yet. A test keeps this table in step with the Compose files: a key marked "in place" must be in the file, and one marked "add this" must not be (until someone adds it and updates this page).

| Compose key | Service (file) | Status | What it does |
|---|---|---|---|
| `cap_drop` | nvr (compose.yaml) | in place | Every Linux capability dropped; the NVR needs none (#384). Disk health adds back only `SYS_RAWIO`. |
| `security_opt` | nvr (compose.yaml) | in place | `no-new-privileges:true`: nothing in the container can gain privileges. |
| `init` | nvr (compose.yaml) | in place | An init process reaps anything a killed worker left (#322). |
| `ulimits` | nvr (compose.yaml) | in place | Open files: 65,536 (#546). |
| `logging` | nvr (compose.yaml) | in place | Logs bounded to 100 MB (#543). |
| `healthcheck` | nvr (compose.yaml) | in place | Docker marks the container unhealthy when `/healthz` fails. |
| `read_only` | nvr (compose.yaml) | add this | A read-only root filesystem. Today the application's folder (`/app`) is owned by the service user, so a compromised worker could change the code the others run. |
| `tmpfs` | nvr (compose.yaml) | add this | With `read_only`, a temporary folder for `/tmp` (`tmpfs: ["/tmp:size=256m"]`). Move the motion buffer off `/tmp` first (`NVR_MOTION_BUFFER_PATH`), or it is held in memory. |
| `pids_limit` | nvr (compose.yaml) | in place | Processes and threads: 16,384 (#546), so a runaway cannot exhaust the host. Threads count: 200 cameras with server-side motion detection pass 4,096. Raise it for a larger site; never lower it. |
| `mem_limit` | nvr (compose.yaml) | add this | See Memory above. |
| `cap_drop` | cloudflared (compose.cloudflared.yaml) | add this | The tunnel container keeps Docker's default capabilities. |
| `security_opt` | cloudflared (compose.cloudflared.yaml) | add this | `no-new-privileges:true`. |
| `security_opt` | tailscale (compose.tailscale.yaml) | add this | `no-new-privileges:true` (it needs `NET_ADMIN` and `NET_RAW`, which it adds). |
| `security_opt` | wireguard (compose.wireguard.yaml) | add this | `no-new-privileges:true` (it needs `NET_ADMIN`, which it adds). |

**The "add this" rows have not been tried with the NVR yet.** Try each on a test machine first:

- **`read_only`:** anything installed into the container by hand (optional AI models under `/opt/nvr/models`) needs a mounted folder instead, and would be lost when the container is recreated anyway.
- **The tunnel images** are third-party. Check that each still starts with `no-new-privileges` before relying on it.

Example for the NVR service, once tried:

```yaml
    read_only: true
    tmpfs:
      - /tmp:size=256m
    mem_limit: 6g   # from your own measurement (Memory above)
```

## 9. Upgrade and rollback

Settings › **Updates** takes a backup first and then shows the exact commands, including the way back ([guided-updates.md](guided-updates.md)). In short:

1. Note the image in use (`docker compose images nvr`). Copy the newest configuration backup off the machine.
2. Set the new version in `compose.yaml`, then `docker compose pull nvr && docker compose up -d nvr`. The first start of a version that changes the database copies it first (`*.pre-migration-*`); it can take a few minutes on a large database. The health check allows five.
3. Check Settings › Updates, **After an upgrade**, and the incident list. Copy that upgrade's pre-migration copy off the machine.
4. **To go back:** stop the container, put back that upgrade's pre-migration copy, set the image you noted, and start it. Footage recorded meanwhile stays; changes the new version made in the database (cases, settings) are lost.

## 10. Sizing

**There is no measured ceiling yet.** The NVR is one web process and one database file. What has been tested is the test suite and CI's simulated camera. The limits it enforces are written down in [console-acceptance.md](console-acceptance.md) and [live-video-ready.md](live-video-ready.md): live tiles per person, streams per server, exports at once, and the rest. Those are bounds the NVR keeps, not a promise that a given host keeps up. No figure for processor, memory or disk speed per camera has been measured.

Until one exists:

- **Estimate with the capacity planner** (Settings › Capacity planner; [capacity-planner.md](capacity-planner.md)).
- **Measure on your own hardware** at full load before relying on it: `docker stats`, the recorder's write latency (`nvr_segment_write_latency_seconds`), the volume's write latency (`nvr_storage_write_latency_seconds`) and dropped segments.
- **Grow one step at a time.** Add cameras in steps and watch the same numbers.

## 11. Monitoring

- [ ] **Scrape `/metrics`** with an API token that has only the `metrics` scope ([metrics-and-log-forwarding.md](metrics-and-log-forwarding.md)). Alert on at least:

  | Alert | Expression (Prometheus) |
  |---|---|
  | A worker is down | `nvr_worker_up == 0` for 5 minutes |
  | A camera stopped writing footage | `time() - nvr_camera_last_footage_write_timestamp_seconds > 300` while `nvr_camera_recording_enabled == 1` |
  | A recording volume is filling | `nvr_storage_free_bytes / nvr_storage_capacity_bytes < 0.1` |
  | A volume is not healthy | `nvr_storage_volume_state{state!="healthy"} == 1` |
  | Backups have stopped | `nvr_backup_stale == 1` |
  | Segments are being dropped | `increase(nvr_segments_dropped_total[1h]) > 0` |

- [ ] **Page someone for these incidents** (Settings › Email alerts, **NVR problems**; or watch `GET /api/incidents`), by fault type:
  - storage, volume and recording: `storage`, `storage_headroom`, `disk_health`, `recording_failure`, `recording_uncertain` (a camera that is offline shows here, with its reason);
  - the NVR itself: `worker_web`, `worker_recorder` and the other `worker_*` types, `ai_worker`, `credential_key`, `backup`, `failover`;
  - moving footage: `tiering` (a failing tier, or one falling behind);
  - security: `signin_guessing`.

  See [incidents.md](incidents.md) and [email-alerts.md](email-alerts.md).
- [ ] **Watch from outside.** If the whole host is down it cannot tell you. Use the signed heartbeat to a receiver elsewhere ([external-heartbeat.md](external-heartbeat.md)), or an uptime check on `/healthz` from another machine.
