"""Take the README's screenshots from demo mode, so anyone can redo them.

  pip install -r requirements-dev.txt && playwright install chromium
  python tools/screenshots.py [out_dir]          default: docs/images

It starts the dashboard in demo mode twice - at the start of the recording,
and 20 minutes in, where a mixture-of-experts model is loaded - and saves one
PNG per view. PLAYWRIGHT_CHANNEL=chrome uses an installed Chrome.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
# (file, demo offset, tab, range, width, height, theme, one card or None)
VIEWS = [
    ("overview.png", 0, "overview", "1h", 1440, 900, "dark", None),
    ("inference.png", 0, "inference", "24h", 1440, 1320, "dark", None),
    ("gpus.png", 0, "gpus", "1h", 1440, 900, "light", None),
    ("anatomy.png", 1200, "flow", "1h", 1440, 900, "dark", "section:has(#arch)"),
    ("phone.png", 0, "overview", "1h", 390, 844, "dark", None),
]


def serve(offset: int, port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "inferenceinquire"],
        cwd=ROOT / "app",
        env={**os.environ, "DEMO": "1", "DEMO_OFFSET": str(offset), "PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    time.sleep(6)  # a few polls, so the live tiles have values
    return proc


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs/images")
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get("PLAYWRIGHT_CHANNEL") or None)
        for offset in sorted({v[1] for v in VIEWS}):
            port = 8950 + offset // 100
            proc = serve(offset, port)
            try:
                for name, off, tab, rng, width, height, theme, card in VIEWS:
                    if off != offset:
                        continue
                    page = browser.new_page(
                        viewport={"width": width, "height": height},
                        color_scheme=theme,
                        reduced_motion="reduce",
                    )
                    page.goto(f"http://127.0.0.1:{port}/#tab={tab}&range={rng}")
                    page.wait_for_timeout(4000)
                    page.mouse.move(0, 0)  # no hover tooltip
                    if card:
                        page.locator(card).screenshot(path=out / name)
                    else:
                        page.screenshot(path=out / name)
                    page.close()
                    print(out / name)
            finally:
                proc.terminate()
                proc.wait(10)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
