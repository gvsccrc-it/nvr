"""Shares of the export and redaction queues, and how a refusal says how long the wait is (#504).

The export queue held four jobs for the whole installation with no share per person: one operator's four jobs stopped everyone's exports and
case packages, possibly for hours. As decided on the ticket, each queue now has a size, a share per person, a time limit for one job, and
takes people in turn; a refusal says how many jobs are ahead and roughly how long until a place frees. The numbers are starting defaults,
each changeable by an environment setting.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from app import env_numbers
from app.clock import parse as clock_parse

TYPICAL_FALLBACK_SECONDS = 300  # What a job is taken to last before any has finished here
TYPICAL_SAMPLE = 20  # Recent finished jobs the typical length is taken from


def setting(name: str, default: int, low: int = 1, high: int = 1000) -> int:
    """A whole-number limit from the environment, kept within ``low`` and ``high``; the default when unset or not a number (app.env_numbers)."""
    return env_numbers.whole_number(name, default, low, high)


def export_limits() -> dict[str, int]:
    return {
        "places": setting("NVR_EXPORT_QUEUE_PLACES", 8, 2),  # At least two: one is kept for case packages
        "each": setting("NVR_EXPORTS_PER_PERSON", 2),
        "each_administrator": setting("NVR_EXPORTS_PER_ADMINISTRATOR", 4),
        "job_hours": setting("NVR_EXPORT_JOB_HOURS", 6, 1, 48),
        "kept_for_case_packages": 1,
    }


def review_limits() -> dict[str, int]:
    return {
        "places": setting("NVR_REDACTION_REVIEW_PLACES", 2),
        "each": setting("NVR_REDACTION_REVIEWS_PER_PERSON", 1),
        "job_hours": setting("NVR_REDACTION_REVIEW_HOURS", 2, 1, 24),
    }


class QueueFull(OverflowError):
    """A request the queue has no place for. ``retry_after`` is a rough number of seconds until one frees, for the Retry-After header."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


def typical_seconds(db: sqlite3.Connection, query: str) -> float:
    """How long a job usually takes here, from ``query``'s (started, finished) pairs of recent jobs; a fallback when none has finished."""
    lengths = []
    for started, finished in db.execute(query, (TYPICAL_SAMPLE,)):
        try:
            lengths.append((clock_parse(finished) - clock_parse(started)).total_seconds())
        except (TypeError, ValueError):
            continue
    lengths = [length for length in lengths if length >= 0]
    return sum(lengths) / len(lengths) if lengths else TYPICAL_FALLBACK_SECONDS


def remaining(started: str | None, typical: float, now: datetime) -> float:
    """Roughly how long a job that started at ``started`` has still to run: at least a minute, so a job past the usual is not 'now'."""
    try:
        elapsed = (now - clock_parse(started)).total_seconds() if started else 0.0
    except (TypeError, ValueError):
        elapsed = 0.0
    return max(60.0, typical - elapsed)


def about(seconds: float) -> str:
    """'about a minute', 'about 25 minutes', 'about 3 hours'."""
    minutes = round(seconds / 60)
    if minutes <= 1:
        return "about a minute"
    if minutes < 90:
        return f"about {minutes} minutes"
    return f"about {round(minutes / 60)} hours"


def jobs(count: int) -> str:
    return f"{count} job{'s' if count != 1 else ''}"
