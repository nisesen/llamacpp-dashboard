"""Serve the dashboard: `python -m inferenceinquire`.

  PORT            the port to listen on (default 8000)
  LISTEN_ADDRESS  the address to listen on (default 0.0.0.0, every interface)

`python -m inferenceinquire --healthcheck` exits 0 when the server answers,
which is what the image's HEALTHCHECK runs.
"""

import os
import sys
import urllib.request

PORT = int(os.environ.get("PORT", "") or 8000)
ADDRESS = os.environ.get("LISTEN_ADDRESS", "") or "0.0.0.0"


def healthcheck() -> int:
    host = "127.0.0.1" if ADDRESS in ("0.0.0.0", "::") else ADDRESS
    try:
        with urllib.request.urlopen(f"http://{host}:{PORT}/healthz", timeout=4) as r:
            return 0 if r.status == 200 else 1
    except OSError:
        return 1


def main() -> int:
    if "--healthcheck" in sys.argv:
        return healthcheck()
    import uvicorn

    uvicorn.run(
        "inferenceinquire.api:app",
        host=ADDRESS,
        port=PORT,
        access_log=False,
        proxy_headers=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
