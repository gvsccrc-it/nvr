"""Each process raises its own open-file limit at start (#546).

A process may open as many files, pipes and connections as its "soft" limit says, and may raise that itself as far as the "hard"
limit. Where Docker runs under systemd the soft limit is commonly 1,024 while the hard limit is far higher. Only the export worker
raised its own (#356). The recorder holds pipes and a process handle for each camera's FFmpeg, plus the cameras' event connections,
and the web worker holds one handle a connection: both could meet 1,024 on a large site, and a process that cannot open a file
cannot open the database either.

``compose.yaml`` sets the container's limit as well; this is for a container started some other way, and costs nothing when the
limit is already high enough.

This module imports nothing of the application: the supervisor calls it before anything else is up.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

def raise_limit() -> int | None:
    """Raise this process's soft open-file limit to the most the system allows (the hard limit); never lowers it. Returns the limit
    now in force, or None when there is none (unlimited) or the system has no such limit to ask about."""
    try:
        import resource
    except ImportError:  # Not a Unix system
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY:
        return None
    target = hard if hard != resource.RLIM_INFINITY else max(soft, 1 << 20)
    if soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError) as exc:
            logger.warning("This process may open %d files at once and could not raise that to %d (%s)", soft, target, exc)
            return soft
        logger.info("Raised this process's open-file limit from %d to %d", soft, target)
        return target
    return soft
