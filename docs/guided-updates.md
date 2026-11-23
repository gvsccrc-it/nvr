# Guided updates

Settings, **Updates** (administrators) tells you when a newer release is out, what it changes, and gives the exact commands to upgrade and to roll back. **The NVR never runs them.** The container has no access to Docker (nothing mounts the Docker socket) and no route can pull an image or restart anything: you copy the commands and run them on the host.

## What it shows

- The running version, the build it is (the commit the image was built from), the exact image name for that build, and the database schema.
- After **Check now**: the newest release newer than this one, its published date, its release notes, and **whether it changes the database** (a database copy is made automatically before migrations run). A release that does not say shows "does not say" rather than a guess.
- After an upgrade, **After an upgrade** lists what the start found: the old and new version and schema and the migrations that were applied, and the most recent migrations in the database.

## The check is opt-out

The check asks one address: the repository's published releases (`https://api.github.com/repos/gvsccrc-it/nvr/releases`). Nothing about the installation is sent. Turn it off with the switch on the page, or for the whole installation with `NVR_UPDATE_CHECK=false`, and then **no request is made at all**: Check now is refused. Opening the page asks once when the last check is more than a day old (or there has been none); Check now asks whenever you like. There is no background checking while nobody has the page open.

| Setting | What it does |
|---|---|
| `NVR_UPDATE_CHECK=false` | Turns the check off for the whole installation. |
| `NVR_UPDATE_FEED_URL` | A different releases feed (a mirror, for an installation without internet). GitHub's releases JSON format; `https`, or `http` to this machine only. |
| `NVR_UPDATE_IMAGE` | The image the commands name. Default `ghcr.io/gvsccrc-it/nvr`. |

## Updating

1. **Back up, then show the upgrade steps.** The configuration backup runs first and is listed (file name and where it is) *before* any command is shown. If it fails, the steps are not shown. If backups are not set up (`NVR_BACKUP_PASSPHRASE`), you have to choose **Show the steps without a backup**.
2. **Before you upgrade, note two things and keep them off the machine.** The image that is running (`docker compose images nvr`, or the image name on the Updates page): it is the way back. And, for an upgrade that changes the database, a good copy of the database: the newest `nvr.sqlite3.pre-migration-*` file beside the database, or the pre-update backup, copied to another machine.
3. **Upgrade, on the host**: set the new image in `compose.yaml`, `docker compose pull nvr`, `docker compose up -d nvr`.
4. **If you need to go back**: stop the container, put back the database copy made just before that upgrade's migrations, set the previous image, and start it. That copy is made when the new version first starts, so before the upgrade the steps say it does not exist yet (the pre-update backup is the way back until then). After the upgrade, **After an upgrade** names the actual copy (the one for that upgrade's schema change, not an older upgrade's) and shows the undo commands with its path. A release that does not change the database needs only the image put back.

**Which image the steps name.** Every build of `main` is published under the tag `sha-<commit>` (the first seven characters of its commit), whether or not it is also a release. The image knows the commit it was built from, each start remembers it, and the rollback steps name the previous image by that tag: `image: ghcr.io/gvsccrc-it/nvr:sha-1a2b3c4`. That tag exists for the image that was running, which a version tag does not unless that version was released. When the NVR does not know the earlier build (it ran an image made before builds were recorded, or one built by hand), the steps say so and tell you to use the image you noted; they offer the version tag only as "if it was that release".

## Checking an image before you run it

Every image CI publishes (`edge`, `sha-<commit>` and each release, which is the same image under another tag) is **signed** by the build itself, with no key anyone keeps (keyless signing with the workflow's GitHub identity). It also carries a record of **how it was built** (provenance) and a **list of what is in it** (SBOM), all attached to its digest (#548). To check an image before you run it, with [cosign](https://docs.sigstore.dev/cosign/system_config/installation/):

```sh
cosign verify ghcr.io/gvsccrc-it/nvr:X.Y.Z \
  --certificate-identity https://github.com/gvsccrc-it/nvr/.github/workflows/ci.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

It succeeds only for an image this repository's CI built on `main`. Anything else fails: an image built elsewhere and pushed under the same name, or a tag moved to another image. Or, with the GitHub CLI, check the build record: `gh attestation verify oci://ghcr.io/gvsccrc-it/nvr:X.Y.Z --owner gvsccrc-it`.

**Releases are named by digest.** A release's line in its notes carries the image's digest, and the upgrade steps the Updates page shows name the image by it (`image: ghcr.io/gvsccrc-it/nvr:X.Y.Z@sha256:…`), with the `cosign verify` command for that digest. Docker then pulls exactly the image CI built, tested and signed, whatever the tag points at later. `release.yml` checks the signature before it tags a release. Images published before #548 are not signed.

**What CI checks in the image.** The build scans the image's system packages (Debian, FFmpeg above all, which parses what cameras send). It fails on any High or Critical finding with a fix available. Findings with no fix yet are listed in the build's log. An exception goes in `.github/vulnerability-exceptions.txt`, with a reason and an expiry date at most 30 days ahead, so it is looked at again. `main` is rebuilt weekly, with the base image's packages upgraded, so `edge` carries Debian's security fixes even when no commit lands. That rebuild publishes `edge` only; `sha-<commit>` stays the image that was tested when the commit was merged.

## Released versions and `edge`

- **A production server runs a released version.** The shipped `compose.yaml` names one (`image: ghcr.io/gvsccrc-it/nvr:X.Y.Z`) and has no `pull_policy: always`, so restarting the stack never upgrades it.
- **`edge` is the build of the latest merge to `main`**, republished on every merge. It is for a test machine only. A server on `edge` with `pull_policy: always` upgrades itself whenever the stack is restarted.
- A new build of the same version (a test machine following `edge`) is listed under **After an upgrade** with the two builds, and the undo commands name the build that ran before.
- `edge` and `sha-<commit>` are published only when the unit tests, the build **and the browser journeys** passed for that commit.

Every path and name placed in a command is quoted for a shell.

Footage is not part of the backup. A rollback restores the database and configuration as they were before the upgrade; footage recorded while the new version ran is still on disk, but anything the new version changed in the database (cases, settings) is lost.

## Publishing a release (for whoever maintains the repository)

Push a tag `vX.Y.Z` on a commit of `main` whose checks all passed (the browser journeys included: the release workflow needs that commit's `sha-<commit>` image, which is published only then), with `APP_VERSION` in `app/version.py` set to `X.Y.Z`. For the next release: raise `APP_VERSION` and the image line in `compose.yaml` together in one pull request, merge it, wait for its checks on `main`, then push the tag on that commit. `.github/workflows/release.yml` publishes `ghcr.io/gvsccrc-it/nvr:X.Y.Z` and creates the release, with generated notes and the line `<!-- nvr-release: {"schema_version": N} -->` that tells installations whether it migrates. Pre-releases and drafts are never offered.

## Limits

- The release workflow could not be run where this was built; its first use is the test of it.
- The commands assume the standard `compose.yaml` with a service named `nvr`.
- "Newer" means a higher `x.y.z`. An installation on the `edge` image reports its `APP_VERSION`, which may be behind or ahead of a tagged release.
- The shipped `compose.yaml` names version `0.1.0`. Until that release is published (its tag pushed), `docker compose pull` on a new machine finds no such image.
- The image knows its commit, not its digest: the steps name the `sha-<commit>` tag. A build made by hand without `--build-arg NVR_BUILD_COMMIT=...` has no build recorded.
