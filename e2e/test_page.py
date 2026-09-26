"""The page in a real browser, against demo mode.

Every tab, at a desktop and a phone width, must load with no console error
and nothing wider than the screen, and a drag across a chart must zoom.

  pip install -r requirements-dev.txt && playwright install chromium
  pytest e2e
DASH_URL points it at a running dashboard instead of starting one;
PLAYWRIGHT_CHANNEL=chrome uses an installed Chrome instead of Playwright's.
"""

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

APP = Path(__file__).resolve().parent.parent / "app"
TABS = ["overview", "inference", "flow", "gpus", "host", "health"]


def demo_server(port, offset=0, agent=True):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=APP,
        env={
            **os.environ,
            "DEMO": "1",
            "DEMO_OFFSET": str(offset),
            "DEMO_AGENT": "1" if agent else "0",
        },
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    time.sleep(3)  # a few polls, so the live buffer has something in it
    return proc, url


@pytest.fixture(scope="session")
def base_url():
    if os.environ.get("DASH_URL"):
        yield os.environ["DASH_URL"].rstrip("/")
        return
    proc, url = demo_server(8931)
    yield url
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="session")
def moe_url():
    """Demo mode 20 minutes in, while the recording serves a mixture-of-experts
    model: the anatomy panel then draws its expert grid."""
    proc, url = demo_server(8932, offset=1200)
    yield url
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="session")
def agentless_url():
    """Demo mode as a dashboard without the host agent sees it: llama.cpp's own
    endpoints only."""
    proc, url = demo_server(8933, agent=False)
    yield url
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(channel=os.environ.get("PLAYWRIGHT_CHANNEL") or None)
        yield b
        b.close()


def open_page(browser, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda e: errors.append(str(e)))
    return page, errors


@pytest.mark.parametrize("width,height", [(1440, 900), (375, 812)], ids=["desktop", "phone"])
def test_every_tab_renders_cleanly(base_url, browser, width, height):
    page, errors = open_page(browser, width, height)
    for tab in TABS:
        page.goto(f"{base_url}/?t={tab}#tab={tab}&range=24h")
        page.wait_for_function("document.querySelectorAll('#tiles .tile').length === 8")
        page.wait_for_timeout(1500)
        wide = page.evaluate("document.documentElement.scrollWidth - innerWidth")
        assert wide <= 0, f"{tab} at {width}px scrolls sideways by {wide}px"
        assert "demo" in page.inner_text("#hostline")
    assert not errors, errors
    page.close()


def test_drag_zooms_and_escape_zooms_out(base_url, browser):
    page, errors = open_page(browser, 1440, 900)
    page.goto(f"{base_url}/#tab=inference&range=1h")
    page.wait_for_function("document.querySelectorAll('#c-decode .u-over').length === 1")
    page.wait_for_timeout(1500)
    # Instantly, not scroll_into_view: the page scrolls smoothly, and a box
    # read mid-scroll puts the drag somewhere else.
    page.evaluate("""() => {
        const r = document.querySelector('#c-decode').getBoundingClientRect();
        window.scrollTo({ top: r.top + scrollY - 200, behavior: 'instant' });
    }""")
    page.wait_for_timeout(500)
    box = page.locator("#c-decode .u-over").bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] * 0.3, y)
    page.wait_for_timeout(200)  # let the chart see the cursor before the press
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.6, y, steps=12)
    page.mouse.up()
    page.wait_for_function("location.hash.includes('from=')")
    assert page.is_visible("#timebanner")
    page.keyboard.press("Escape")
    page.wait_for_function("!location.hash.includes('from=')")
    assert not errors, errors
    page.close()


@pytest.mark.parametrize("width,height", [(1440, 900), (375, 812)], ids=["desktop", "phone"])
def test_the_expert_grid_fits(moe_url, browser, width, height):
    page, errors = open_page(browser, width, height)
    page.goto(f"{moe_url}/#tab=flow&range=1h")
    page.wait_for_function("document.querySelectorAll('#arch .experts i').length > 100")
    page.wait_for_timeout(1000)
    wide = page.evaluate("document.documentElement.scrollWidth - innerWidth")
    assert wide <= 0, f"the flow tab at {width}px scrolls sideways by {wide}px"
    assert not errors, errors
    page.close()


@pytest.mark.parametrize("width,height", [(1440, 900), (375, 812)], ids=["desktop", "phone"])
def test_without_the_agent_only_what_works_is_shown(agentless_url, browser, width, height):
    """No hardware tabs, no per-request cards, the counter tiles and charts
    drawn from llama.cpp's own counters, one line saying why - and no errors."""
    page, errors = open_page(browser, width, height)
    page.goto(f"{agentless_url}/#tab=overview&range=1h")
    page.wait_for_function("document.querySelectorAll('#tiles .tile').length === 6")
    assert page.is_visible(".agent-note")
    shown = page.eval_on_selector_all(
        "#navlinks a", "as => as.filter(a => a.offsetParent).map(a => a.dataset.tab)"
    )
    assert shown == ["overview", "inference", "flow", "health", "all"]
    for tab in ["overview", "inference", "flow", "health", "all"]:
        page.goto(f"{agentless_url}/?t={tab}#tab={tab}&range=1h")
        page.wait_for_timeout(1200)
        wide = page.evaluate("document.documentElement.scrollWidth - innerWidth")
        assert wide <= 0, f"{tab} at {width}px scrolls sideways by {wide}px"
        visible = page.eval_on_selector_all(
            "[data-needs~=agent]", "els => els.filter(e => e.offsetParent).length"
        )
        assert visible == 0, f"{tab}: {visible} agent-only elements shown"
    page.goto(f"{agentless_url}/#tab=inference&range=1h")
    page.wait_for_function("document.querySelectorAll('#c-decode .u-over').length === 1")
    assert "host agent" not in page.inner_text("#footer-text")
    assert not errors, errors
    page.close()
