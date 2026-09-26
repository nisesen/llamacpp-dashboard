"""Make the data demo mode (DEMO=1) replays, with what identifies a machine scrubbed.

  python tools/demo_data.py record <seconds> <out.jsonl.xz>
  python tools/demo_data.py scrub <in.jsonl.xz> <out.jsonl.xz>
  python tools/demo_data.py history <metrics.db> <out.json.xz> [--hostname NAME ...]

record runs the dashboard's own poll loop against a live agent and llama.cpp
(configured as usual: AGENT_URL, AGENT_TOKEN, LLAMA_URL,
LLAMA_API_KEY) and saves every response: an xz'd JSONL file with a header
line, each distinct body once, then one line per poll. The result is scrubbed.

scrub does the scrubbing on its own, for a recording made without it.

history exports the last 7 days of a dashboard database: 30-second rows for
the last day (the energy figures need them), 5-minute rows before that, and
the requests, events and decode observations, all aggregated by the same rules
as the dashboard's own rollups.

Scrubbed: the host name, every IPv4 address (to 192.0.2.0/24, keeping ports),
GPU serials and UUIDs, and guest names. Nothing else is: read the output
before you publish it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import lzma
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
GPU_UUID = re.compile(r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Scrubber:
    """Replacements learned from the data itself, applied consistently."""

    def __init__(self, hostnames=()):
        self.text: dict[str, str] = {h: "gpu-host" for h in hostnames if h}
        self.ips: dict[str, str] = {}
        self.guests: dict[str, str] = {}

    def learn(self, agent: dict) -> None:
        host = (agent.get("static") or {}).get("hostname")
        if host:
            self.text.setdefault(host, "gpu-host")
        gpus = (agent.get("gpu") or {}).get("gpus") or []
        for n, g in enumerate(sorted(gpus, key=lambda g: g.get("index") or 0), 1):
            if g.get("serial"):
                self.text.setdefault(str(g["serial"]), f"13200000000{n:02d}")
            if g.get("uuid"):
                self.text.setdefault(g["uuid"], f"GPU-00000000-0000-0000-0000-{n:012d}")
        for g in (agent.get("guests") or {}).get("guests") or []:
            if g.get("name"):
                self.guests.setdefault(g["name"], f"{g.get('type') or 'guest'}-{g.get('vmid')}")

    def _ip(self, m: re.Match) -> str:
        ip = m.group(0)
        if ip.startswith(("127.", "0.")) or ip == "255.255.255.255":
            return ip
        if not all(0 <= int(p) <= 255 for p in ip.split(".")):
            return ip  # a version number or similar, not an address
        return self.ips.setdefault(ip, f"192.0.2.{len(self.ips) + 10}")

    def text_(self, s: str) -> str:
        for old, new in sorted(self.text.items(), key=lambda kv: -len(kv[0])):
            s = s.replace(old, new)
        # A UUID nothing taught us (a card seen only in a process list): generic,
        # but never one of the placeholders already substituted above.
        done = set(self.text.values())
        s = GPU_UUID.sub(
            lambda m: (
                m.group(0)
                if m.group(0) in done
                else self.text.setdefault(
                    m.group(0), f"GPU-00000000-0000-0000-0000-{90 + len(self.text):012d}"
                )
            ),
            s,
        )
        return IPV4.sub(self._ip, s)

    def agent(self, body: str) -> str:
        d = json.loads(body)
        self.learn(d)
        for g in (d.get("guests") or {}).get("guests") or []:
            if g.get("name") in self.guests:
                g["name"] = self.guests[g["name"]]
        return self.text_(json.dumps(d, separators=(",", ":")))


# ----------------------------------------------------------------- record ---


def record(seconds: float, out: Path) -> None:
    sys.path.insert(0, str(APP))
    import os

    os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "record.db"))
    import httpx
    from inferenceinquire import config
    from inferenceinquire.monitor import Monitor
    from inferenceinquire.store import Store

    frames: list[dict] = []
    bodies: dict[str, str] = {}
    current: list[dict] = []

    class Tee(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            try:
                resp = await self.inner.handle_async_request(request)
                body = await resp.aread()
            except Exception as exc:
                current.append({"u": str(request.url), "e": type(exc).__name__, "msg": str(exc)})
                raise
            h = hashlib.sha1(body).hexdigest()
            bodies[h] = body.decode("utf-8", "replace")
            current.append(
                {
                    "u": str(request.url),
                    "s": resp.status_code,
                    "ct": resp.headers.get("content-type"),
                    "b": h,
                }
            )
            keep = [
                (k, v)
                for k, v in resp.headers.items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
            ]
            return httpx.Response(resp.status_code, headers=keep, content=body, request=request)

    async def run():
        mon = Monitor(Store(config.DB_PATH))
        mon.client = httpx.AsyncClient(transport=Tee())
        end = time.time() + seconds
        while time.time() < end:
            t0 = time.perf_counter()
            current.clear()
            t, pc = time.time(), time.perf_counter()
            try:
                await mon.poll_once()
            except Exception as exc:
                print("poll failed:", exc, file=sys.stderr)
            frames.append({"t": t, "pc": pc, "r": list(current)})
            await asyncio.sleep(max(0.05, config.POLL_INTERVAL - (time.perf_counter() - t0)))

    asyncio.run(run())
    header = {"header": 1, "started": frames[0]["t"], "poll_interval": config.POLL_INTERVAL}
    write_session(out, header, bodies, frames)
    scrub(out, out)


def read_session(path: Path):
    header, bodies, frames = None, {}, []
    with lzma.open(path, "rt") as fh:
        for line in fh:
            row = json.loads(line)
            if "header" in row:
                header = row
            elif "body" in row:
                bodies[row["body"]] = row["text"]
            else:
                frames.append(row)
    return header, bodies, frames


def write_session(path: Path, header, bodies, frames) -> None:
    used = {r["b"] for f in frames for r in f["r"] if "b" in r}
    with lzma.open(path, "wt", preset=9 | lzma.PRESET_EXTREME) as fh:
        fh.write(json.dumps(header) + "\n")
        for h in sorted(used):
            fh.write(json.dumps({"body": h, "text": bodies[h]}) + "\n")
        for f in frames:
            fh.write(json.dumps(f) + "\n")


# ------------------------------------------------------------------ scrub ---


def scrub(src: Path, out: Path) -> None:
    header, bodies, frames = read_session(src)
    s = Scrubber()
    agent_bodies = {
        r["b"] for f in frames for r in f["r"] if "b" in r and r["u"].endswith("/metrics.json")
    }
    new_bodies: dict[str, str] = {}
    renamed: dict[str, str] = {}
    # Agent payloads first: they teach the scrubber the names to replace.
    for h in sorted(agent_bodies):
        text = s.agent(bodies[h])
        renamed[h] = hashlib.sha1(text.encode()).hexdigest()
        new_bodies[renamed[h]] = text
    for h, text in bodies.items():
        if h in agent_bodies:
            continue
        text = s.text_(text)
        renamed[h] = hashlib.sha1(text.encode()).hexdigest()
        new_bodies[renamed[h]] = text
    for f in frames:
        for r in f["r"]:
            r["u"] = s.text_(r["u"])
            if "b" in r:
                r["b"] = renamed[r["b"]]
            if "msg" in r:
                r["msg"] = s.text_(r["msg"])
    header = {k: header[k] for k in ("header", "started", "poll_interval") if k in header}
    write_session(out, header, new_bodies, frames)
    print(
        f"scrubbed {len(frames)} polls; replaced {len(s.text)} names, {len(s.ips)} addresses, "
        f"{len(s.guests)} guest names -> {out} ({out.stat().st_size // 1024} KB)"
    )


# ---------------------------------------------------------------- history ---


def history(db: Path, out: Path, hostnames=()) -> None:
    sys.path.insert(0, str(APP))
    from inferenceinquire.schema import GPU_COLS, SAMPLE_COLS, rollup_select

    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    end = c.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
    day, week = end - 86400, end - 7 * 86400
    cols = [x for x in SAMPLE_COLS if x != "ts"]
    s = Scrubber(hostnames)

    def rows(sql, args):
        return [
            [round(v, 3) if isinstance(v, float) else v for v in r] for r in c.execute(sql, args)
        ]

    def buckets(width, lo, hi):
        b = f"CAST(ts / {width} AS INTEGER) * {width}"
        samples = rows(
            f"SELECT {b} AS bts, {rollup_select(cols)} FROM samples "
            "WHERE ts >= ? AND ts < ? GROUP BY bts ORDER BY bts",
            (lo, hi),
        )
        gpus = rows(
            f"SELECT {b} AS bts, idx, {rollup_select(GPU_COLS, gpu=True)} "
            "FROM gpu_samples WHERE ts >= ? AND ts < ? GROUP BY bts, idx "
            "ORDER BY bts, idx",
            (lo, hi),
        )
        return samples, gpus

    old_s, old_g = buckets(300, week, day)
    new_s, new_g = buckets(30, day, end + 1)
    req_cols = [r[1] for r in c.execute("PRAGMA table_info(requests)") if r[1] != "id"]
    data = {
        "end": end,
        "samples": {"cols": ["ts", *cols], "rows": old_s + new_s},
        "gpu_samples": {"cols": ["ts", "idx", *GPU_COLS], "rows": old_g + new_g},
        "requests": {
            "cols": req_cols,
            "rows": rows(
                f"SELECT {','.join(req_cols)} FROM requests WHERE finished >= ? ORDER BY finished",
                (week,),
            ),
        },
        "events": {
            "cols": ["ts", "level", "key", "title", "detail", "state"],
            "rows": [
                [ts, lvl, key, s.text_(title or ""), s.text_(detail or ""), state]
                for ts, lvl, key, title, detail, state in rows(
                    "SELECT ts, level, key, title, detail, state FROM events WHERE ts >= ? "
                    "ORDER BY ts",
                    (week,),
                )
            ],
        },
        "decode_obs": {
            "cols": ["ts", "model", "tps"],
            "rows": rows(
                "SELECT ts, model, tps FROM (SELECT ts, model, tps, ROW_NUMBER() OVER "
                "(PARTITION BY model ORDER BY ts DESC) AS n FROM decode_obs) WHERE n <= 300 "
                "ORDER BY ts",
                (),
            ),
        },
    }
    with lzma.open(out, "wt", preset=9 | lzma.PRESET_EXTREME) as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(
        {k: len(v["rows"]) for k, v in data.items() if isinstance(v, dict)},
        f"-> {out} ({out.stat().st_size // 1024} KB)",
    )


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["-h"]
    if cmd == "record":
        record(float(args[0]), Path(args[1]))
    elif cmd == "scrub":
        scrub(Path(args[0]), Path(args[1]))
    elif cmd == "history":
        names = args[args.index("--hostname") + 1 :] if "--hostname" in args else []
        history(Path(args[0]), Path(args[1]), names)
    else:
        print(__doc__)
