"""Demo mode: a recorded session replayed as if it were happening now.

DEMO=1 needs no llama.cpp server, no GPU and no agent. The poll loop runs
exactly as it does against a real machine, but its HTTP client answers from
app/demo/session.jsonl.xz, and the store starts with a week of history from
app/demo/history.json.xz, moved to end now. tools/demo_data.py makes both.

The session loops. Each pass moves every timestamp forward by the session's
length and carries cumulative counters on from where the last pass ended, so
nothing downstream sees time or a counter go backwards.
"""

from __future__ import annotations

import bisect
import json
import lzma
import re
import time
from pathlib import Path

import httpx

from . import config

DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"

# Keys that hold a moment in time, anywhere in the agent's payload. The BMC log
# keeps its own dates: those events really are that old.
TS_KEYS = {"ts", "started", "finished", "active_since", "counters_since"}
KEEP_TIME = {("sel",)}
# Where the agent's cumulative counters live.
COUNTERS = (
    ("cpu",),
    ("net", "ifaces"),
    ("disk_io",),
    ("journal", "counters"),
    ("journal", "lines_seen"),
    ("journal", "matched"),
    ("uptime",),
    ("guests", "llm_ct", "uptime"),
)
METRIC_LINE = re.compile(r"^(\S+_total(?:\{[^}]*\})?) (\S+)$", re.M)


def _leaves(x, path=()):
    """Every numeric leaf of a JSON value, with its path."""
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _leaves(v, (*path, k))
    elif isinstance(x, list):
        for n, v in enumerate(x):
            yield from _leaves(v, (*path, n))
    elif isinstance(x, (int, float)) and not isinstance(x, bool):
        yield path, x


class Session:
    """The recording, and how to serve any moment of it at any later time."""

    def __init__(self, path: Path = DEMO_DIR / "session.jsonl.xz"):
        self.bodies: dict[str, str] = {}
        self.frames: list[dict] = []
        with lzma.open(path, "rt") as fh:
            for line in fh:
                row = json.loads(line)
                if "header" in row:
                    self.poll_interval = row.get("poll_interval", 2.0)
                elif "body" in row:
                    self.bodies[row["body"]] = row["text"]
                else:
                    self.frames.append(row)
        self.t0 = self.frames[0]["t"]
        self.offsets = [f["t"] - self.t0 for f in self.frames]
        self.period = self.offsets[-1] + self.poll_interval
        self.started = time.time() - config.DEMO_OFFSET
        self.agent_step = self._steps(self._agent(0), self._agent(-1))
        self.metric_step = self._metric_steps(self._body(0, "/metrics"), self._body(-1, "/metrics"))

    def _body(self, i, path):
        for r in self.frames[i]["r"]:
            if "b" in r and httpx.URL(r["u"]).path == path:
                return self.bodies[r["b"]]
        return ""

    def _agent(self, i):
        return json.loads(self._body(i, "/metrics.json") or "{}")

    @staticmethod
    def _steps(first, last):
        a, b = dict(_leaves(first)), dict(_leaves(last))
        return {
            p: b[p] - v
            for p, v in a.items()
            if p in b and any(p[: len(c)] == c for c in COUNTERS) and b[p] >= v
        }

    @staticmethod
    def _metric_steps(first, last):
        a = dict(METRIC_LINE.findall(first))
        b = dict(METRIC_LINE.findall(last))
        return {
            k: float(b[k]) - float(v) for k, v in a.items() if k in b and float(b[k]) >= float(v)
        }

    def moment(self, now: float) -> tuple[int, int]:
        """(pass, frame) for a wall-clock time."""
        k, pos = divmod(max(0.0, now - self.started), self.period)
        return int(k), max(0, bisect.bisect_right(self.offsets, pos) - 1)

    def answer(self, k: int, i: int, path: str):
        """The recorded answer for a path at frame i, moved to pass k."""
        for j in range(i, -1, -1):  # a path not read at this poll: its last answer
            for r in self.frames[j]["r"]:
                if httpx.URL(r["u"]).path == path:
                    return r, self._shift(r, k, path)
        return None, None

    def _shift(self, r, k, path):
        if "b" not in r:
            return None
        body = self.bodies[r["b"]]
        if path == "/metrics.json":
            return json.dumps(self._shift_agent(json.loads(body), k))
        if path == "/metrics" and k:
            return METRIC_LINE.sub(
                lambda m: (
                    f"{m.group(1)} {float(m.group(2)) + k * self.metric_step.get(m.group(1), 0)}"
                ),
                body,
            )
        return body

    def _shift_agent(self, d, k):
        delta = self.started + k * self.period - self.t0
        steps = self.agent_step

        def walk(x, path):
            if isinstance(x, dict):
                return {key: walk(v, (*path, key)) for key, v in x.items()}
            if isinstance(x, list):
                return [walk(v, (*path, n)) for n, v in enumerate(x)]
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                if path and path[-1] in TS_KEYS and path[:1] not in KEEP_TIME:
                    return x + delta
                if k and path in steps:
                    return x + k * steps[path]
            return x

        return walk(d, ())


class ReplayTransport(httpx.AsyncBaseTransport):
    """Answers the poll loop from the session. Every request of one poll is
    answered from the same recorded moment: the agent's, which comes first."""

    def __init__(self, session: Session):
        self.session = session
        self.at: tuple[int, int] | None = None

    async def handle_async_request(self, request):
        path = request.url.path
        if path == "/metrics.json" or self.at is None:
            self.at = self.session.moment(time.time())
        r, body = self.session.answer(*self.at, path)
        if r is None:
            return httpx.Response(404, text="not in the demo recording", request=request)
        if "e" in r:
            raise httpx.ConnectError(r.get("msg", "demo: no answer"), request=request)
        headers = {"content-type": r["ct"]} if r.get("ct") else {}
        return httpx.Response(
            r["s"], headers=headers, content=(body or "").encode(), request=request
        )


def replay_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ReplayTransport(Session()))


# The columns that hold a time, per table in the history file.
TIME_COLS = {
    "samples": ("ts",),
    "gpu_samples": ("ts",),
    "requests": ("started", "finished"),
    "events": ("ts",),
    "decode_obs": ("ts",),
}


def seed(store, now: float, path: Path = DEMO_DIR / "history.json.xz") -> None:
    """Fill an empty store with the recorded week, moved to end just before now."""
    if store.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]:
        return
    with lzma.open(path, "rt") as fh:
        data = json.load(fh)
    shift = now - config.POLL_INTERVAL - data["end"]
    for table, times in TIME_COLS.items():
        cols, rows = data[table]["cols"], data[table]["rows"]
        idx = [cols.index(c) for c in times if c in cols]
        moved = []
        for row in rows:
            row = list(row)
            for n in idx:
                if row[n] is not None:
                    row[n] += shift
            moved.append(row)
        store.conn.executemany(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", moved
        )
    store.conn.commit()
