"""Live-viewing admission limits shared by layout validation and media delivery."""
from __future__ import annotations

from app import env_numbers

MAX_LIVE_VIEWERS = 16
MAX_LIVE_AUDIO_LISTENERS = 16
# Live streams re-encoded on this server, at once: an H.265 camera converted to H.264 for a browser that cannot play it, and a camera whose
# privacy mask the NVR draws in (#488). Each is a full decode and encode, shared by every viewer of that stream.
MAX_LIVE_TRANSCODES = env_numbers.whole_number("NVR_LIVE_CONVERSIONS", 6, 1, 64)
