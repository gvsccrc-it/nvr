"""Release identifier reported by the API and status endpoints, and the commit the running image was built from."""
from __future__ import annotations

import os
import re

APP_VERSION = "0.1.0"


def build_commit() -> str:
    """The commit this image was built from (the Dockerfile's NVR_BUILD_COMMIT, set by CI), or "" when it is not known: a build made by
    hand, or the source run outside an image. Only a plain commit id is believed: it ends up in commands people paste into a shell."""
    value = os.environ.get("NVR_BUILD_COMMIT", "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{7,40}", value) else ""


def build_tag(commit: str | None = None) -> str | None:
    """The image tag CI publishes for every build of main, whatever else it is called (edge, or a version): sha-<the commit's first seven>."""
    commit = build_commit() if commit is None else commit
    return f"sha-{commit[:7]}" if commit else None
