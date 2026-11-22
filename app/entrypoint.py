"""Container entrypoint: validates the listener and runs the worker supervisor."""
from __future__ import annotations

import os


def listen_port() -> int:
    raw = os.environ.get("NVR_LISTEN_PORT", "8280")
    try:
        port = int(raw)
    except ValueError as exc:
        raise SystemExit("NVR_LISTEN_PORT must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise SystemExit("NVR_LISTEN_PORT must be an integer from 1 to 65535")
    return port


LISTEN_HOST_HELP = ("NVR_LISTEN_HOST must be one IP address of this machine, such as 127.0.0.1 (this machine only) or 192.0.2.10 "
                    "(the management network's address), or 0.0.0.0 for every address")


def listen_host() -> str:
    """The address the web interface is served on (#541). Every IPv4 address when it is not set, as it always was: the NVR cannot know
    which of the host's addresses people reach it by. A name or anything else that is not an address stops start-up with a plain message:
    a typo here must not quietly open the interface on every network."""
    import ipaddress

    raw = os.environ.get("NVR_LISTEN_HOST", "").strip()
    if not raw:
        return "0.0.0.0"
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError as exc:
        raise SystemExit(LISTEN_HOST_HELP) from exc


def every_address(host: str) -> bool:
    return host in {"0.0.0.0", "::"}


def health_url() -> str:
    """Where the container's own health check asks: the address the interface is served on, or loopback when that is every address."""
    host = listen_host()
    host = "127.0.0.1" if every_address(host) else host
    return f"http://{f'[{host}]' if ':' in host else host}:{listen_port()}/healthz"


def main() -> None:
    listen_port()
    listen_host()
    from app.workers.supervisor import main as supervise

    raise SystemExit(supervise())


if __name__ == "__main__":
    main()
