"""Turning llama.cpp's service log into state.

llama.cpp's log carries far more than /metrics does: a per-slot generation
rate every ~3 s, per-request draft acceptance, and the slot routing decision
including the prompt-cache similarity score. Parsing it turns the dashboard
from a sampler into an event stream: completed requests, live slots, the
model-load record, and counters for signals /metrics does not carry.

JournalParser only consumes lines, so it runs on a captured log as happily as
on a live one. follower.py owns the journalctl process that feeds it.
"""

import collections
import threading
import time

from .patterns import (
    RE_AUTH_FAIL,
    RE_CACHE_EVICT,
    RE_CACHE_SKIP,
    RE_CANCEL,
    RE_DRAFT,
    RE_EVAL,
    RE_GRAPHS,
    RE_LAUNCH,
    RE_LOADED,
    RE_LOADING,
    RE_OOM,
    RE_PICK_LCP,
    RE_PICK_LRU,
    RE_PROMPT,
    RE_RELEASE,
    RE_TG,
    RE_TOTAL,
)


def fresh_load():
    """The model-load record before anything is known."""
    return {"state": "unknown", "started": None, "finished": None, "path": None, "warnings": []}


class JournalParser:
    """Keeps what the log says: requests, slots, the load record, counters."""

    MAX_EVENTS = 220
    MAX_REQUESTS = 60
    # A load still unfinished after this is an abandoned one, not a slow one:
    # even very large models load in a minute or two from local NVMe.
    LOAD_STALL_AFTER = 300.0

    def __init__(self):
        self.lock = threading.Lock()
        self.events = collections.deque(maxlen=self.MAX_EVENTS)
        self.requests = collections.deque(maxlen=self.MAX_REQUESTS)
        self.slots = {}  # id -> live per-slot state
        self.pending = {}  # task -> partial request record
        self.load = fresh_load()
        self.lines_seen = 0
        self.started_at = time.time()
        self.last_line_at = None
        # Monotonic since agent start; the dashboard differences them. Counted
        # against a high-water mark because every respawn replays the last 40
        # lines (`-n 40`), and a re-read burst must not count twice.
        self.counters = {
            "auth_fail": 0,
            "cache_evict": 0,
            "cache_evict_mib": 0.0,
            "cache_skip": 0,
            "cancel": 0,
        }
        self.count_hwm = 0.0
        self.cache_limit_mib = None
        # Lines each pattern has matched since start. The dashboard compares
        # them with llama.cpp's own counters to notice a log format it no
        # longer reads.
        self.matched = collections.Counter()

    def reset_load(self):
        with self.lock:
            self.load = fresh_load()

    def _ts(self, line):
        head = line.split(" ", 1)[0]
        try:
            return float(head)
        except ValueError:
            return time.time()

    def _emit(self, ts, kind, slot, task, detail):
        self.events.append({"ts": ts, "kind": kind, "slot": slot, "task": task, "detail": detail})

    def _slot(self, sid):
        return self.slots.setdefault(int(sid), {"id": int(sid)})

    def _count(self, ts, key, amount=1):
        """Count a log signal once, however many times the line is re-read."""
        if ts <= self.count_hwm:
            return False
        self.count_hwm = ts
        self.counters[key] += amount
        return True

    def ingest(self, line):
        """Read one journal line. The first pattern that matches handles it."""
        ts = self._ts(line)
        with self.lock:
            self.lines_seen += 1
            self.last_line_at = time.time()
            for name, rx, handle in self.PATTERNS:
                m = rx.search(line)
                if m:
                    self.matched[name] += 1
                    handle(self, ts, m, line)
                    return

    # ---- one handler per pattern, called with the lock held ---------------

    def _auth_fail(self, ts, m, line):
        self._count(ts, "auth_fail")

    def _cache_evict(self, ts, m, line):
        if self._count(ts, "cache_evict"):
            self.counters["cache_evict_mib"] += float(m.group(1))

    def _cache_skip(self, ts, m, line):
        self.cache_limit_mib = float(m.group(2))
        self._count(ts, "cache_skip")

    def _cancel(self, ts, m, line):
        self._count(ts, "cancel")

    def _tg(self, ts, m, line):
        sid, task, n_gen, tg, tg3 = m.groups()
        s = self._slot(sid)
        s.update(
            {
                "task": int(task),
                "n_gen": int(n_gen),
                "tg": float(tg),
                "tg_3s": float(tg3),
                "phase": "decode",
                "ts": ts,
            }
        )

    def _prompt(self, ts, m, line):
        sid, task, ms, toks, _mspt, tps = m.groups()
        r = self.pending.setdefault(int(task), {"task": int(task), "slot": int(sid)})
        r.update(
            {
                "prompt_ms": float(ms),
                "prompt_tokens": int(toks),
                "prefill_tps": float(tps),
                "ts": ts,
            }
        )
        self._slot(sid).update({"phase": "decode", "ts": ts})

    def _eval(self, ts, m, line):
        sid, task, ms, toks, _mspt, tps = m.groups()
        r = self.pending.setdefault(int(task), {"task": int(task), "slot": int(sid)})
        r.update(
            {
                "eval_ms": float(ms),
                "gen_tokens": int(toks),
                "decode_tps": float(tps),
                "ts": ts,
            }
        )

    def _total(self, ts, m, line):
        sid, task, ms, toks = m.groups()
        r = self.pending.setdefault(int(task), {"task": int(task), "slot": int(sid)})
        r.update({"total_ms": float(ms), "total_tokens": int(toks)})

    def _draft(self, ts, m, line):
        sid, task, rate, acc, gen, mean = m.groups()
        r = self.pending.setdefault(int(task), {"task": int(task), "slot": int(sid)})
        r.update(
            {
                "accept_rate": float(rate),
                "accepted": int(acc),
                "drafted": int(gen),
                "mean_len": float(mean),
            }
        )

    def _graphs(self, ts, m, line):
        sid, task, n = m.groups()
        r = self.pending.setdefault(int(task), {"task": int(task), "slot": int(sid)})
        r["graphs_reused"] = int(n)

    def _launch(self, ts, m, line):
        sid, task, child = m.groups()
        s = self._slot(sid)
        pick = s.pop("next_pick", None)
        s.update(
            {
                "task": int(task),
                "phase": "prefill",
                "ts": ts,
                "n_gen": 0,
                "started": ts,
                "pick": pick,
                "is_child": child == "1",
            }
        )
        self.pending[int(task)] = {
            "task": int(task),
            "slot": int(sid),
            "started": ts,
            "pick": pick,
        }
        self._emit(ts, "launch", int(sid), int(task), (pick or {}).get("how", "assigned"))

    def _release(self, ts, m, line):
        sid, task, n_tokens, trunc = m.groups()
        s = self._slot(sid)
        s.update({"phase": "idle", "ts": ts, "task": None})
        r = self.pending.pop(int(task), {"task": int(task), "slot": int(sid)})
        r.update({"n_tokens": int(n_tokens), "truncated": trunc != "0", "finished": ts})
        if r.get("started"):
            r["wall_s"] = ts - r["started"]
        self.requests.appendleft(r)
        self._emit(ts, "release", int(sid), int(task), f"{r.get('gen_tokens', '?')} tokens")

    def _pick_lcp(self, ts, m, line):
        sid, sim, keep = m.groups()
        self._slot(sid)["next_pick"] = {
            "how": "cache",
            "similarity": float(sim),
            "keep": float(keep),
        }

    def _pick_lru(self, ts, m, line):
        sid, _t = m.groups()
        self._slot(sid)["next_pick"] = {"how": "lru"}

    def _loading(self, ts, m, line):
        self.load = {
            "state": "loading",
            "started": ts,
            "finished": None,
            "path": m.group(1),
            "warnings": [],
        }
        self.slots.clear()
        self.pending.clear()
        self._emit(ts, "load", None, None, "loading model")

    def _loaded(self, ts, m, line):
        self.load["state"] = "loaded"
        self.load["finished"] = ts
        if self.load.get("started"):
            self.load["seconds"] = ts - self.load["started"]
        self._emit(ts, "load", None, None, "model loaded")

    def _oom(self, ts, m, line):
        w = line.split(": ", 2)[-1][:200]
        if w not in self.load["warnings"]:
            self.load["warnings"].append(w)
            self._emit(ts, "warning", None, None, w)

    # In the order they are tried. A line counts against the first match only.
    PATTERNS = (
        ("auth_fail", RE_AUTH_FAIL, _auth_fail),
        ("cache_evict", RE_CACHE_EVICT, _cache_evict),
        ("cache_skip", RE_CACHE_SKIP, _cache_skip),
        ("cancel", RE_CANCEL, _cancel),
        ("tg", RE_TG, _tg),
        ("prompt", RE_PROMPT, _prompt),
        ("eval", RE_EVAL, _eval),
        ("total", RE_TOTAL, _total),
        ("draft", RE_DRAFT, _draft),
        ("graphs", RE_GRAPHS, _graphs),
        ("launch", RE_LAUNCH, _launch),
        ("release", RE_RELEASE, _release),
        ("pick_lcp", RE_PICK_LCP, _pick_lcp),
        ("pick_lru", RE_PICK_LRU, _pick_lru),
        ("loading", RE_LOADING, _loading),
        ("loaded", RE_LOADED, _loaded),
        ("oom", RE_OOM, _oom),
    )

    def _load_view(self, now):
        """The load record, with an abandoned load demoted. Caller holds lock.

        Backstop for the case _spawn cannot see: a unit stopped mid-load and
        never restarted, so there is no new journal line to correct the record.
        """
        load = dict(self.load)
        if (
            load.get("state") == "loading"
            and load.get("started")
            and now - load["started"] > self.LOAD_STALL_AFTER
        ):
            load["state"] = "aborted"
        return load

    def snapshot(self):
        with self.lock:
            now = time.time()
            slots = []
            for _sid, s in sorted(self.slots.items()):
                age = now - s.get("ts", 0)
                d = dict(s)
                d["stale"] = age > 12  # no line for a while
                d["age"] = round(age, 1)
                if d["stale"] and d.get("phase") == "decode":
                    d["phase"] = "idle"
                slots.append(d)
            return {
                "last_line_age": (round(now - self.last_line_at, 1) if self.last_line_at else None),
                "lines_seen": self.lines_seen,
                "slots": slots,
                "events": list(self.events)[-80:],
                "requests": list(self.requests)[:24],
                "load": self._load_view(now),
                "counters": dict(self.counters),
                "counters_since": self.started_at,
                "cache_limit_mib": self.cache_limit_mib,
                "matched": {name: self.matched[name] for name, _, _ in self.PATTERNS},
            }
