"""Live-viewing admission limits shared by layout validation and media delivery."""
from __future__ import annotations

from app import env_numbers

# The most tiles a live layout has (a 4 x 4 grid).
MAX_LIVE_TILES = 16
# Live-view places (#487). Each identity has its own share: a person signed in, an API token, a video wall. One cannot take them all, and
# watching a stream already open for someone else costs the server nothing new, so the server-wide bound is on distinct camera streams.
MAX_LIVE_VIEWERS = env_numbers.whole_number("NVR_LIVE_TILES_PER_PERSON", 16, 1, 256)  # Live tiles one person (or API token) may have open
MAX_LIVE_VIEWERS_WALL = env_numbers.whole_number("NVR_LIVE_TILES_PER_WALL", 16, 1, 256)  # Live tiles one video wall may have open
MAX_LIVE_STREAMS = env_numbers.whole_number("NVR_LIVE_STREAMS", 32, 1, 1024)  # Distinct camera streams open for viewers at once
MAX_MJPEG_EACH = env_numbers.whole_number("NVR_LIVE_MJPEG_PER_PERSON", 2, 0, 64)  # Compatibility (MJPEG) tiles: each a decode and encode of its own
MAX_MJPEG = env_numbers.whole_number("NVR_LIVE_MJPEG", 4, 0, 256)
MAX_LIVE_AUDIO_LISTENERS = 16
# Live streams re-encoded on this server, at once: an H.265 camera converted to H.264 for a browser that cannot play it, and a camera whose
# privacy mask the NVR draws in (#488). Each is a full decode and encode, shared by every viewer of that stream.
MAX_LIVE_TRANSCODES = env_numbers.whole_number("NVR_LIVE_CONVERSIONS", 6, 1, 64)
