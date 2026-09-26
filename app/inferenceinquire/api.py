"""The HTTP and WebSocket API, and the page itself."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .alerts import should_notify
from .monitor import Monitor
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx logs every request at INFO - several lines per poll - which buries the
# warnings that matter and rotates them out of the container log within hours.
logging.getLogger("httpx").setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


store = Store(config.DB_PATH)
monitor = Monitor(store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.DEMO:
        from .demo import seed

        seed(store, time.time())
    task = asyncio.create_task(monitor.run())
    yield
    task.cancel()


app = FastAPI(title="InferenceInquire", version=__version__, lifespan=lifespan)
# JSON compresses ~5-10x, and a 7-day request history was 604 KB per fetch.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# HEAD too: some uptime checkers probe with it, and got 405.
@app.api_route("/healthz", methods=["GET", "HEAD"], response_class=PlainTextResponse)
async def healthz():
    return "ok"


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    """The dashboard's own numbers, so Grafana can scrape this later."""
    snap = monitor.snapshot
    pt = monitor.points[-1] if monitor.points else {}
    lines: list[str] = []

    def emit(name, value, labels="", help_="", type_="gauge"):
        if value is None:
            return
        if help_:
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {type_}")
        lines.append(f"{name}{labels} {value}")

    emit(
        "llmdash_up",
        1 if snap.get("llama", {}).get("healthy") else 0,
        help_="1 when the inference server reports healthy",
    )
    emit("llmdash_agent_up", 1 if snap.get("agent_ok") else 0)
    emit(
        "llmdash_decode_tokens_per_second",
        pt.get("decode_tps"),
        help_="decode rate of the last completed request",
    )
    emit("llmdash_prefill_tokens_per_second", pt.get("prefill_tps"))
    emit(
        "llmdash_decode_baseline_tokens_per_second",
        pt.get("decode_baseline"),
        help_="learned median decode rate for the loaded model",
    )
    emit("llmdash_requests_processing", pt.get("req_proc"))
    emit(
        "llmdash_live_tokens_per_second",
        pt.get("tg_live"),
        help_="llama.cpp's own 3s windowed generation rate",
    )
    emit("llmdash_disk_read_bytes_per_second", pt.get("disk_read"))
    emit("llmdash_context_tokens", pt.get("ctx_used"))
    for level in ("critical", "serious", "warning"):
        n = sum(1 for a in snap.get("alerts", []) if a["level"] == level)
        emit("llmdash_alerts", n, f'{{level="{level}"}}')
    for g in snap.get("gpus", []):
        lbl = f'{{gpu="{g["idx"]}",serial="{g.get("serial") or ""}"}}'
        emit("llmdash_gpu_temp_celsius", g.get("temp"), lbl)
        emit("llmdash_gpu_memory_temp_celsius", g.get("hbm"), lbl)
        emit("llmdash_gpu_power_watts", g.get("pwr"), lbl)
        emit("llmdash_gpu_power_limit_watts", g.get("pwr_limit"), lbl)
        emit(
            "llmdash_gpu_utilization_ratio",
            (g.get("util") or 0) / 100 if g.get("util") is not None else None,
            lbl,
        )
        emit("llmdash_gpu_pcie_rx_megabytes_per_second", g.get("pcie_rx"), lbl)
        emit("llmdash_gpu_pcie_tx_megabytes_per_second", g.get("pcie_tx"), lbl)
        emit(
            "llmdash_gpu_memory_used_bytes",
            (g.get("mem_used") or 0) * 1024 * 1024 if g.get("mem_used") else None,
            lbl,
        )
    for n, (pattern, count) in enumerate(sorted(monitor.journal_matched.items())):
        emit(
            "llmdash_journal_lines_matched_total",
            count,
            f'{{pattern="{pattern}"}}',
            help_="journal lines each agent pattern has matched since the agent started"
            if n == 0
            else "",
            type_="counter",
        )
    return "\n".join(lines) + "\n"


@app.get("/api/state")
async def api_state():
    return JSONResponse({"snap": monitor.snapshot, "points": list(monitor.points)[-60:]})


RANGES = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
    "14d": 1209600,
    "30d": 2592000,
    "90d": 7776000,
    "1y": 31536000,
}


def window_end(end: float | None) -> float:
    """The end of the window a request asks for: now, or a pinned moment
    (a frozen view, or a link to one), never in the future."""
    now = time.time()
    return min(end, now) if end else now


@app.get("/api/series")
async def api_series(range: str = Query("1h"), end: float | None = None):
    seconds = RANGES.get(range, 3600)
    until = window_end(end)
    bucket = max(config.POLL_INTERVAL * config.PERSIST_EVERY, seconds / 900)
    return JSONResponse(store.series(until - seconds, bucket, until))


@app.get("/api/usage")
async def api_usage(range: str = Query("24h"), tz: float = 0.0, end: float | None = None):
    """Energy and usage for the page's range, never less than a day: hourly
    buckets up to 24 h, calendar days beyond. `tz` is the reader's offset
    from UTC in minutes, so a day is their day."""
    seconds = max(RANGES.get(range, 86400), 86400)
    bucket = 3600.0 if seconds <= 86400 else 86400.0 if seconds <= 90 * 86400 else 7 * 86400.0
    off = tz * 60
    now = window_end(end)
    n = round(seconds / bucket)
    first = math.floor((now + off) / bucket) * bucket - off - (n - 1) * bucket
    return JSONResponse(store.usage(first, now, bucket, off))


@app.get("/api/requests")
async def api_requests(
    range: str = Query("6h"), since: float | None = None, end: float | None = None
):
    """Completed requests in the range - or, with `since`, only those after it,
    so a page that already holds the range fetches what is new, not all of it.
    `end` pins the window's end (a frozen view)."""
    until = window_end(end)
    start = until - RANGES.get(range, 21600)
    if since is not None:
        start = max(start, since)
    return JSONResponse(store.requests_since(start, until=until))


@app.get("/api/events")
async def api_events(limit: int = 80, since: float | None = None):
    """Newest first - or, with `since`, everything after it in time order,
    which is what the charts' event markers are built from."""
    if since is not None:
        return JSONResponse(store.events_since(since, max(1, min(limit, 20000))))
    return JSONResponse(store.recent_events(limit))


@app.get("/api/alerts")
async def api_alerts(since: float | None = None):
    """What an alert forwarder polls (see examples/forwarder/).

    `alerts` is the current state. `events` are the push-worthy raise/clear
    edges after `since`, so a condition that came and went between two polls
    is still delivered. `poll_age` lets the forwarder tell a dashboard that
    answers HTTP but has stopped polling from one that is working.
    """
    snap = monitor.snapshot
    now = time.time()
    events = []
    if since is not None:
        events = [
            dict(e, notify=True)
            for e in store.events_since(since)
            if should_notify(e["key"], e["level"])
        ]
    return JSONResponse(
        {
            "ts": now,
            "poll_age": (now - monitor.last_poll_ok) if monitor.last_poll_ok else None,
            "worst": snap.get("worst", "good"),
            "model": (snap.get("model") or {}).get("alias"),
            "alerts": snap.get("alerts", []),
            "transition": snap.get("transition"),
            "events": events,
        }
    )


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        # Registered only after init, and with the signatures of the snapshot
        # it was sent - read together, so its first tick is slimmed correctly.
        snap, sigs = monitor.snapshot, monitor.sigs
        await ws.send_text(
            json.dumps(
                {
                    "type": "init",
                    "points": list(monitor.points),
                    "snap": snap,
                    "meta": {
                        "version": __version__,
                        "poll_interval": config.POLL_INTERVAL,
                        "live_minutes": config.LIVE_MINUTES,
                        "retain_days": config.RETAIN_DAYS,
                        "ranges": list(RANGES),
                    },
                },
                default=str,
            )
        )
        monitor.clients[ws] = sigs
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        monitor.clients.pop(ws, None)


@app.get("/manifest.webmanifest")
async def manifest():
    """What makes the page installable: "Add to Home Screen" opens it full
    screen, with its own icon, like an app. Icons from tools/make_icons.py."""

    def icon(src, size, kind, purpose):
        return {"src": src, "sizes": size, "type": kind, "purpose": purpose}

    return JSONResponse(
        {
            "name": "InferenceInquire",
            "short_name": "InferenceInquire",
            "description": "Health, hardware and inference telemetry for a llama.cpp box",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0d0d0d",
            "theme_color": "#0d0d0d",
            "icons": [
                icon("/static/icons/icon-192.png", "192x192", "image/png", "any"),
                icon("/static/icons/icon-512.png", "512x512", "image/png", "any"),
                # Full-bleed with the mark inside the central 60%: safe to mask.
                icon("/static/icons/icon-192.png", "192x192", "image/png", "maskable"),
                icon("/static/icons/icon-512.png", "512x512", "image/png", "maskable"),
                icon("/static/icons/icon.svg", "any", "image/svg+xml", "any"),
            ],
        },
        media_type="application/manifest+json",
    )


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def index():
    html = (STATIC_DIR / "index.html").read_text()
    if not config.AGENT_ENABLED:
        # Known before any script runs, so what needs the agent is never built.
        html = html.replace("<html ", '<html data-agent="off" ', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Make browsers revalidate /static on every load.

    StaticFiles already sends ETag/Last-Modified, so revalidation costs one
    304 - but without this header a browser will happily serve a stale app.js
    for days after a redeploy, which looks exactly like a broken deploy.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
