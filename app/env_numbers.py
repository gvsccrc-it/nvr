"""Numbers read from the environment, with a default and limits (#544).

Several settings were turned into numbers as their module loaded, with nothing to catch a slip: ``NVR_STOP_GRACE_SECONDS=30s`` raised
``ValueError`` in the supervisor, the container's first process, and the restart policy looped it with only a traceback to go on. Zero and
negative values were taken as they were: a stop grace of 0 killed every worker at once.

``number`` never raises. A value that is not a number gives the default; one outside the limits gives the nearest limit. Either way one
line in the log names the variable and the value used. The rejected text is not printed: an environment value can be anything.

This module imports nothing of the application: the supervisor reads its settings before anything else is up.
"""
from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)
_said: set[str] = set()  # One line a variable a process, however often the setting is read


def number(name: str, default: float, least: float, most: float, *, whole: bool = False) -> float:
    """The environment's ``name`` as a number between ``least`` and ``most``: ``default`` when it is unset, empty or not a number."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default) if whole else float(default)
    try:
        value = float(raw.strip())
        if not math.isfinite(value):
            raise ValueError(raw)
        used, why = value, None
        if value < least:
            used, why = least, "is below the least allowed"
        elif value > most:
            used, why = most, "is above the most allowed"
    except ValueError:
        used, why = default, "is not a number"
    used = int(used) if whole else float(used)
    if why is not None and name not in _said:
        _said.add(name)
        logger.warning("The setting %s %s; %s is used", name, why, used)
    return used


def whole_number(name: str, default: int, least: int, most: int) -> int:
    return int(number(name, default, least, most, whole=True))
