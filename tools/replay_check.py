"""Prove a refactor changed nothing: replay a recording through two versions of
the dashboard and compare everything they produce.

  python tools/replay_check.py <git-ref> [recording]

The recording defaults to app/demo/session.jsonl.xz (see tools/demo_data.py).
Each side runs the real poll loop - Monitor.run() - in its own interpreter,
with a fake clock set to each recorded poll, asyncio.sleep replaced by "go to
the next poll", and an HTTP client that answers from the recording. Compared:
every WebSocket tick a client receives, the HTTP API's answers at checkpoints
(through the ASGI app, gzip and routing included), the log messages, and every
row of every table at the end. It prints IDENTICAL, or the first differences.

This is how the split into modules was checked, and it is the check to run before
merging anything that should not change behaviour.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- one side ---


def replay(rec_path: str, app_dir: str, out_path: str) -> None:
    import asyncio
    import importlib
    import logging
    import lzma
    import sqlite3
    import threading
    import time

    header, bodies, frames = {}, {}, []
    with lzma.open(rec_path, "rt") as fh:
        for line in fh:
            row = json.loads(line)
            if "header" in row:
                header = row
            elif "body" in row:
                bodies[row["body"]] = row["text"]
            else:
                frames.append(row)

    clock = {
        "t": header.get("monitor_started", frames[0]["t"]),
        "pc": header.get("started_pc", frames[0].get("pc", 0.0)),
    }
    main = threading.main_thread()
    time.time = lambda: clock["t"]
    time.perf_counter = lambda: clock["pc"]

    first = frames[0]["r"]
    agent = next(r["u"] for r in first if r["u"].endswith("/metrics.json")).rsplit("/", 1)[0]
    llama = next(r["u"] for r in first if r["u"].endswith("/health")).rsplit("/", 1)[0]
    for k, v in (header.get("env") or {}).items():
        if v is not None:
            os.environ[k] = v
    os.environ.pop("DEMO", None)
    os.environ.update(
        {
            # Both names: before 0.1.0 the agent's settings were HOST_AGENT_*.
            "AGENT_URL": agent,
            "AGENT_TOKEN": "t",
            "HOST_AGENT_URL": agent,
            "HOST_AGENT_TOKEN": "t",
            "LLAMA_URL": llama,
            "LLAMA_API_KEY": "k",
            "DB_PATH": os.path.join(tempfile.mkdtemp(), "replay.db"),
        }
    )
    out = open(out_path, "w")  # noqa: SIM115 - closed at the end, after many writes

    def emit(kind, value):
        out.write(json.dumps({"k": kind, "v": value}, default=str) + "\n")

    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.INFO and not record.name.startswith("http"):
                emit("log", [record.levelname, record.getMessage()])

    sys.path.insert(0, app_dir)
    import httpx

    real_client = httpx.AsyncClient
    frame = {"i": 0, "queue": []}
    misses = []

    class Replay(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            url = str(request.url)
            for n, r in enumerate(frame["queue"]):
                if r["u"] == url:
                    frame["queue"].pop(n)
                    break
            else:
                misses.append([frame["i"], url])
                raise httpx.ConnectError(f"replay: nothing recorded for {url}", request=request)
            if "e" in r:
                raise getattr(httpx, r["e"], httpx.ConnectError)(r["msg"], request=request)
            headers = {"content-type": r["ct"]} if r.get("ct") else {}
            return httpx.Response(
                r["s"], headers=headers, content=bodies[r["b"]].encode(), request=request
            )

    httpx.AsyncClient = lambda *a, **kw: real_client(*a, transport=Replay(), **kw)
    logging.getLogger().addHandler(Capture())
    logging.getLogger().setLevel(logging.INFO)

    server = importlib.import_module("server")
    monitor = (
        getattr(server, "monitor", None) or importlib.import_module("inferenceinquire.api").monitor
    )
    from starlette.testclient import TestClient

    client = TestClient(server.app)  # no `with`: the lifespan's real poll loop never starts
    t0 = frames[0]["t"]

    class FakeWS:
        async def send_text(self, text):
            emit("tick", text)

    monitor.clients[FakeWS()] = {}

    def checkpoint(i):
        for u in (
            "/api/state",
            "/api/series?range=5m",
            "/api/series?range=1h",
            "/api/series?range=24h",
            "/api/series?range=7d",
            f"/api/series?range=1h&end={t0 + 900}",
            "/api/usage?range=24h&tz=-420",
            "/api/usage?range=7d&tz=-420",
            "/api/requests?range=6h",
            f"/api/requests?range=1h&since={t0 + 600}",
            "/api/events?limit=80",
            f"/api/events?since={t0 - 60}&limit=20000",
            "/api/alerts",
            f"/api/alerts?since={t0 - 60}",
            "/metrics",
            "/healthz",
            "/manifest.webmanifest",
        ):
            r = client.get(u, headers={"accept-encoding": "gzip"})
            emit("http", [i, u, r.status_code, r.headers.get("content-type"),
                          r.headers.get("content-encoding"), r.text])  # fmt: skip
        with client.websocket_connect("/ws") as ws:
            emit("ws-init", [i, ws.receive_text()])

    class Done(Exception):
        pass

    real_sleep = asyncio.sleep

    def load(i):
        f = frames[i]
        clock["t"], clock["pc"] = f["t"], f.get("pc", clock["pc"])
        frame["queue"] = list(f["r"])

    async def fake_sleep(delay, *a, **kw):
        if threading.current_thread() is not main or delay <= 0:
            return await real_sleep(delay, *a, **kw)
        i = frame["i"]
        if i % 150 == 149:
            checkpoint(i)
        frame["i"] = i = i + 1
        if i >= len(frames):
            raise Done
        load(i)

    asyncio.sleep = fake_sleep

    async def run():
        load(0)
        try:
            await monitor.run()
        except Done:
            pass

    asyncio.run(run())
    asyncio.sleep = real_sleep
    checkpoint(len(frames))
    emit("misses", misses)
    emit("points", list(monitor.points))
    emit("snapshot", monitor.snapshot)
    db = sqlite3.connect(os.environ["DB_PATH"])
    for name, sql in db.execute("SELECT name, sql FROM sqlite_master ORDER BY type, name"):
        emit("schema", [name, sql])
        if sql and sql.startswith("CREATE TABLE"):
            cols = [c[1] for c in db.execute(f"PRAGMA table_info({name})")]
            order = ", ".join(f'"{c}"' for c in cols)
            emit(
                "table",
                [name, cols, db.execute(f"SELECT * FROM {name} ORDER BY {order}").fetchall()],
            )
    out.close()
    print(f"{app_dir}: {len(frames)} polls replayed, {len(misses)} unanswered requests")


# ------------------------------------------------------------- comparison ---


def compare(a_path: Path, b_path: Path) -> bool:
    a, b = a_path.read_text().splitlines(), b_path.read_text().splitlines()
    kinds = collections.Counter(json.loads(x)["k"] for x in a)
    print("compared:", dict(kinds))
    diffs = [i for i in range(max(len(a), len(b))) if a[i : i + 1] != b[i : i + 1]]
    if not diffs:
        print("IDENTICAL")
        return True
    print(f"{len(diffs)} lines differ; the first:")

    def parse(line):
        v = json.loads(line)
        if isinstance(v["v"], str):
            try:
                v["v"] = json.loads(v["v"])
            except ValueError:
                pass
        return v

    def walk(x, y, path=""):
        if type(x) is not type(y):
            yield path, x, y
        elif isinstance(x, dict):
            for k in sorted(set(x) | set(y), key=str):
                yield from walk(x.get(k, "<missing>"), y.get(k, "<missing>"), f"{path}.{k}")
        elif isinstance(x, list):
            if len(x) != len(y):
                yield path + "[len]", len(x), len(y)
            for n, (p, q) in enumerate(zip(x, y)):
                yield from walk(p, q, f"{path}[{n}]")
        elif x != y:
            yield path, x, y

    for i in diffs[:5]:
        x = parse(a[i]) if i < len(a) else {}
        y = parse(b[i]) if i < len(b) else {}
        print(f"--- line {i} ({x.get('k')})")
        for n, (p, u, v) in enumerate(walk(x, y)):
            if n == 6:
                break
            print(f"   {p}: {str(u)[:120]} != {str(v)[:120]}")
    return False


def main() -> int:
    if sys.argv[1:2] == ["--replay"]:
        replay(*sys.argv[2:5])
        return 0
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ref = sys.argv[1]
    rec = Path(sys.argv[2] if len(sys.argv) > 2 else ROOT / "app/demo/session.jsonl.xz").resolve()
    work = Path(tempfile.mkdtemp(prefix="replay-check-"))
    archive = work / "ref.tar"
    subprocess.run(["git", "-C", str(ROOT), "archive", "-o", str(archive), ref, "app"], check=True)
    with tarfile.open(archive) as t:
        t.extractall(work / "ref", filter="data")
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    outs = []
    for label, app_dir in ((ref, work / "ref/app"), ("working tree", ROOT / "app")):
        out = work / f"{len(outs)}.jsonl"
        print(f"replaying through {label} ...", flush=True)
        subprocess.run([sys.executable, __file__, "--replay", str(rec), str(app_dir), str(out)],
                       env=env, check=True)  # fmt: skip
        outs.append(out)
    return 0 if compare(*outs) else 1


if __name__ == "__main__":
    sys.exit(main())
