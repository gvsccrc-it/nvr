"""What the NVR's processes are given of its environment, and where its secrets come from (#547).

The backup passphrase, the sign-in secrets and the log-forwarding headers sat in the container's environment, the supervisor copied the
whole of it to all six workers, and every FFmpeg, ffprobe and smartctl process inherited it: FFmpeg parses what cameras send, so a flaw in
it was a flaw in a process holding the backup passphrase.

- ``child_environment()`` is the environment every media and disk-health process is started with: a short, listed set of variables
  (``CHILD_NAMES`` and the hardware-acceleration prefixes in ``CHILD_PREFIXES``), never one whose name looks like a secret.
- ``worker_environment(name)`` is what the supervisor gives a worker: everything but the secrets that worker does not use (``SECRETS_FOR``).
- ``secret(name)`` reads a secret from the variable, or from the file its ``_FILE`` variable names (a Docker or Compose secret), so a
  secret need not be in the environment at all.

Children run as the same user as the workers, so this narrows what a flaw exposes; it does not remove it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# The basics every program expects, and the time zone the recorder sets for its segment names.
CHILD_NAMES = frozenset({"PATH", "HOME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "LD_LIBRARY_PATH", "XDG_RUNTIME_DIR"})
# Hardware acceleration (docs/hevc-and-acceleration.md): VAAPI and Quick Sync drivers (libva, oneVPL/Media SDK) and NVIDIA's CUDA read these.
# The devices themselves are passed on FFmpeg's command line.
CHILD_PREFIXES = ("LC_", "LIBVA_", "MFX_", "ONEVPL_", "INTEL_MEDIA_", "CUDA_", "NVIDIA_")
LOOKS_SECRET = re.compile(r"SECRET|PASSPHRASE|PASSWORD|TOKEN|HEADERS|CREDENTIAL|_KEY\b|KEY_PATH|AUTH", re.IGNORECASE)

# Each secret, and the workers that use it. Every other worker is started without it (and without its _FILE form).
SECRETS_FOR = {
    "NVR_OIDC_CLIENT_SECRET": {"web"},  # Microsoft Entra sign-in: only the web worker serves it
    "NVR_OIDC_SESSION_SECRET": {"web"},
    "NVR_BACKUP_PASSPHRASE": {"web", "background"},  # Backups, restores, failover sync and their status: the web worker and the background one
}


def child_environment(**extra: str) -> dict[str, str]:
    """The environment for a media or disk-health process: the listed variables that are set, and ``extra``."""
    found = {name: value for name, value in os.environ.items()
             if (name in CHILD_NAMES or name.startswith(CHILD_PREFIXES)) and not LOOKS_SECRET.search(name)}
    return {**found, **extra}


def worker_environment(worker: str) -> dict[str, str]:
    """The environment for one worker: the supervisor's, without the secrets that worker does not use."""
    environment = dict(os.environ)
    for name, users in SECRETS_FOR.items():
        if worker not in users:
            environment.pop(name, None)
            environment.pop(f"{name}_FILE", None)
    return environment


def secret(name: str) -> str:
    """A secret from the variable ``name``, exactly as set (as it was always read: a backup made with it must still open), or else from the
    file ``name_FILE`` names, whose surrounding whitespace (its trailing newline above all) is not part of it; empty when neither gives one."""
    value = os.environ.get(name, "")
    if value:
        return value
    path = os.environ.get(f"{name}_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
