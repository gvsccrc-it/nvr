"""The container's health check: ``python -m app.healthcheck`` asks the NVR's own /healthz and exits 0 when it answers (#541).

It asks at the address the interface is served on. The check used to be written out in the Dockerfile and the Compose file with
127.0.0.1 in it, which nothing answers on once the interface is bound to one other address. It imports nothing of the application
beyond the two settings: it runs every 30 seconds.
"""
from __future__ import annotations

import sys
import urllib.request

from app.entrypoint import health_url


def main() -> int:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # Never through a proxy: this is the machine asking itself
        with opener.open(health_url(), timeout=3) as response:
            return 0 if response.status == 200 else 1
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - whatever went wrong, the answer is "not healthy"
        print(f"unhealthy: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
