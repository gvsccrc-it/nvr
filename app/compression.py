"""JSON answers are sent compressed to browsers that accept it (#537).

Nothing the app sent was compressed, and the WireGuard and Tailscale set-ups in the remote-access guide have no proxy to do it: the camera
list, fetched every 15 seconds, sent every camera as it is (about 1 kB a camera), where JSON compresses eight to ten times. The interface
files are compressed once at build time and served from there (``app.api.web_interface``); this is for JSON answers, by the owner's
decision on #537:

- only an answer to a GET, with a JSON media type, of at least ``MINIMUM_BYTES``, sent whole (not streamed), not already
  encoded, to a client whose ``Accept-Encoding`` takes gzip;
- never video, picture streams, line-by-line JSON (``application/x-ndjson``), downloads or anything streamed: they are not JSON, or are
  sent in pieces, and are passed through untouched, as is everything over a WebSocket;
- never an answer that carries a secret shown once (a new API token, its signing secret, second-factor enrolment, recovery codes,
  a native sign-in): compressing a secret next to text a caller can influence is a known way of leaking it (BREACH). Every such answer is
  to a POST, and only GETs are compressed; a GET that ever holds one says so with ``no_compression``.

gzip at level 5: about a millisecond for the 200-camera list. A proxy in front may compress too; the app never compresses an answer that is
already encoded.
"""
from __future__ import annotations

import gzip
from typing import Any

from fastapi import Request

MINIMUM_BYTES = 1024
LEVEL = 5
STATE_FLAG = "nvr_no_compression"


def no_compression(request: Request) -> None:
    """A dependency for a route whose answer must never be compressed (it carries a secret shown once)."""
    request.state.nvr_no_compression = True


def _accepts_gzip(scope: dict[str, Any]) -> bool:
    for name, value in scope.get("headers") or []:
        if name == b"accept-encoding":
            for item in value.decode("latin-1").split(","):
                coding, _, parameters = item.strip().partition(";")
                if coding.strip().lower() in {"gzip", "*"} and parameters.replace(" ", "").lower() not in {"q=0", "q=0.0", "q=0.00", "q=0.000"}:
                    return True
    return False


def _is_json(media: str) -> bool:
    kind = media.split(";", 1)[0].strip().lower()
    return kind == "application/json" or (kind.startswith("application/") and kind.endswith("+json"))


class JsonCompression:
    def __init__(self, app: Any, minimum: int = MINIMUM_BYTES) -> None:
        self.app, self.minimum = app, minimum

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "GET" or not _accepts_gzip(scope):
            await self.app(scope, receive, send)
            return
        held: dict[str, Any] | None = None
        decided = False

        async def wrapped(message: dict[str, Any]) -> None:
            nonlocal held, decided
            if decided:
                await send(message)
                return
            if message["type"] == "http.response.start":
                held = message
                return
            if message["type"] != "http.response.body" or held is None:
                await send(message)
                return
            decided = True
            headers = [(name.lower(), value) for name, value in held.get("headers", [])]
            names = {name for name, _ in headers}
            media = next((value.decode("latin-1") for name, value in headers if name == b"content-type"), "")
            body = message.get("body", b"")
            whole = not message.get("more_body", False)
            secret = bool((scope.get("state") or {}).get(STATE_FLAG))
            if _is_json(media) and whole and not secret and b"content-encoding" not in names and len(body) >= self.minimum:
                compressed = gzip.compress(body, compresslevel=LEVEL, mtime=0)
                kept = [(name, value) for name, value in held.get("headers", []) if name.lower() not in {b"content-length", b"vary"}]
                vary = [value.decode("latin-1") for name, value in headers if name == b"vary"]
                kept += [(b"content-encoding", b"gzip"), (b"content-length", str(len(compressed)).encode()),
                         (b"vary", ", ".join([*vary, "Accept-Encoding"]).encode())]
                await send({**held, "headers": kept})
                await send({**message, "body": compressed})
                return
            await send(held)
            await send(message)

        await self.app(scope, receive, wrapped)
        if held is not None and not decided:
            await send(held)  # A start with no body after it (nothing else the app sends): passed on as it came
