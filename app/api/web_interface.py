"""The browser interface: one built page for every client route, plus its hashed assets.

Built assets live in app/web_dist (produced by the web/ Vite project in the container
build). Hashed assets are cached for a year; the page itself is always revalidated.
The router is registered after every API router, so its catch-all never shadows them;
unknown /api paths still answer with JSON 404s. The former /next preview paths redirect.
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

router = APIRouter()
DIST_DIRECTORY = Path(__file__).resolve().parent.parent / "web_dist"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}
# Paths owned by the server, never by client routing.
SERVER_PREFIXES = ("api/", "assets/", "sw.js", "manifest.webmanifest", "openapi.json", "docs", "redoc")
_LEADING_SEPARATORS = re.compile(r"^[\s/\\\x00-\x1f\x7f]+")


def _page() -> FileResponse:
    page = DIST_DIRECTORY / "index.html"
    if not page.is_file():
        raise HTTPException(status_code=503, detail="The web interface has not been built")
    return FileResponse(page, media_type="text/html", headers={**SECURITY_HEADERS, "Cache-Control": "no-cache"})


def interface_version() -> str:
    """Which build of the browser interface this server gives out: a short digest of its page, which names every hashed asset, so it
    changes whenever the interface does. "" when the interface has not been built. A page left open across an upgrade (a video wall)
    compares this with what it saw when it loaded, and reloads itself."""
    page = DIST_DIRECTORY / "index.html"
    try:
        return hashlib.sha256(page.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


@router.get("/next", include_in_schema=False)
@router.get("/next/{path:path}", include_in_schema=False)
def preview_redirect(request: Request, path: str = "") -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    # Always a path on this NVR: "//host", "/\host" or "/<tab>/host" would send the browser to another site (#271).
    path = _LEADING_SEPARATORS.sub("", path)
    return RedirectResponse(f"/{path}{query}", status_code=308)


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> Response:
    """The installable app's service worker (web/public/sw.js), versioned by the built page."""
    page, worker = DIST_DIRECTORY / "index.html", DIST_DIRECTORY / "sw.js"
    if not page.is_file() or not worker.is_file():
        raise HTTPException(status_code=503, detail="The web interface has not been built")
    version = interface_version()
    return Response(worker.read_text(encoding="utf-8").replace("__VERSION__", version), media_type="text/javascript; charset=utf-8",
                    headers={**SECURITY_HEADERS, "Cache-Control": "no-cache"})


# The copies the build made of each file (web/vite.config.ts, #537), best first: Brotli, then gzip
_ENCODINGS = (("br", ".br"), ("gzip", ".gz"))


def _accepted(header: str) -> set[str]:
    """The content codings an Accept-Encoding header takes (a quality of 0 refuses one)."""
    taken = set()
    for item in header.split(","):
        coding, _, parameters = item.strip().partition(";")
        if parameters.replace(" ", "").lower() not in {"q=0", "q=0.0", "q=0.00", "q=0.000"}:
            taken.add(coding.strip().lower())
    return taken


@router.get("/assets/{path:path}", include_in_schema=False)
def asset(path: str, request: Request) -> FileResponse:
    candidate = (DIST_DIRECTORY / "assets" / path).resolve()
    if (DIST_DIRECTORY / "assets").resolve() not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    headers = {**SECURITY_HEADERS, "Cache-Control": "public, max-age=31536000, immutable", "Vary": "Accept-Encoding"}
    # The copy compressed at build time, for a browser that takes it (#537): no work here, and a fraction of the bytes
    taken = _accepted(request.headers.get("accept-encoding", ""))
    for coding, suffix in _ENCODINGS:
        compressed = candidate.with_name(candidate.name + suffix)
        if (coding in taken or "*" in taken) and compressed.is_file():
            media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            return FileResponse(compressed, media_type=media_type, headers={**headers, "Content-Encoding": coding})
    return FileResponse(candidate, headers=headers)


@router.get("/", include_in_schema=False)
@router.get("/{path:path}", include_in_schema=False)
def interface(path: str = "") -> FileResponse:
    if path.startswith(SERVER_PREFIXES):
        raise HTTPException(status_code=404, detail="Not found")
    return _page()
