"""SQLite persistence: samples, requests, events, learned baselines, rollups
and retention.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import statistics
import time
from pathlib import Path

from . import config
from .schema import GPU_COLS, SAMPLE_COLS, SCHEMA, history_source, rollup_select
from .usage import usage_report
from .workload import is_probe

log = logging.getLogger(__name__)


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # Tables that were here before this start: only their changes are news.
        self.existing = {
            r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not self.existing:
            log.info("created a new database at %s", path)
        self.conn.executescript(SCHEMA)
        for name in config.ROLLUPS:
            self.conn.execute(f"CREATE TABLE IF NOT EXISTS samples_{name} (ts REAL PRIMARY KEY)")
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS gpu_samples_{name} "
                f"(ts REAL, idx INTEGER, PRIMARY KEY (ts, idx))"
            )
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns this build expects but an older database lacks.

        The dashboard grows new metrics as the machine does; CREATE TABLE IF
        NOT EXISTS would silently keep an old shape and every insert would
        fail. Cheap and idempotent.
        """
        tables = [("samples", SAMPLE_COLS), ("gpu_samples", GPU_COLS), ("requests", ["probe"])]
        for name in config.ROLLUPS:  # rollups carry every column raw history does
            tables += [(f"samples_{name}", SAMPLE_COLS), (f"gpu_samples_{name}", GPU_COLS)]
        for table, cols in tables:
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
                    if table in self.existing:
                        log.info("migrated: %s.%s added", table, col)
        # Requests stored before the column existed, or before a probe
        # fingerprint was configured.
        if config.PROBE_GEN_TOKENS is None:
            n = self.conn.execute("UPDATE requests SET probe = 0 WHERE probe IS NULL").rowcount
        else:
            n = self.conn.execute(
                "UPDATE requests SET probe = (gen_tokens = ? AND COALESCE(n_tokens, 0) < ?) "
                "WHERE probe IS NULL",
                (config.PROBE_GEN_TOKENS, config.PROBE_MAX_TOKENS),
            ).rowcount
        if n:
            log.info("migrated: classified %d stored requests as probe/workload", n)

    def get_kv(self, key: str, default=None):
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_kv(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def write(self, point: dict, gpus: list[dict]) -> None:
        self.conn.execute(
            f"INSERT OR REPLACE INTO samples ({','.join(SAMPLE_COLS)}) "
            f"VALUES ({','.join('?' * len(SAMPLE_COLS))})",
            [point.get(c) for c in SAMPLE_COLS],
        )
        # Built from GPU_COLS, exactly like the samples statement above and
        # like series() below. A hand-written column list desynchronises the
        # moment a metric is added to GPU_COLS - _migrate() widens the table,
        # the bindings grow, the frozen statement does not, and every write
        # then raises. That is not hypothetical: adding pcie_rx/pcie_tx broke
        # GPU history for ~19 hours and only showed up as one WARNING line.
        gcols = ["ts", "idx", *GPU_COLS]
        placeholders = ",".join("?" * len(gcols))
        for g in gpus:
            self.conn.execute(
                f"INSERT OR REPLACE INTO gpu_samples ({','.join(gcols)}) VALUES ({placeholders})",
                [point["ts"], g["idx"]] + [g.get(c) for c in GPU_COLS],
            )
        self.conn.commit()

    # ---- learned decode baseline, per model -----------------------------

    def record_decode(self, ts: float, model: str, tps: float) -> None:
        self.conn.execute("INSERT INTO decode_obs VALUES (?,?,?)", (ts, model, tps))
        self.conn.commit()

    def decode_baseline(self, model: str) -> tuple[float | None, int]:
        """Median decode rate for this model, over its last N observations.

        A median, not a mean: if the box really does fall back to CPU for a
        while, those samples must not drag the baseline down to meet them.
        """
        rows = self.conn.execute(
            "SELECT tps FROM decode_obs WHERE model=? ORDER BY ts DESC LIMIT ?",
            (model, config.BASELINE_WINDOW),
        ).fetchall()
        vals = [r[0] for r in rows if r[0] is not None]
        if len(vals) < config.MIN_BASELINE_SAMPLES:
            return None, len(vals)
        return statistics.median(vals), len(vals)

    REQ_COLS = (
        "task",
        "slot",
        "model",
        "started",
        "finished",
        "wall_s",
        "prompt_tokens",
        "prefill_tps",
        "gen_tokens",
        "decode_tps",
        "accept_rate",
        "mean_len",
        "n_tokens",
        "cache_similarity",
        "pick",
        "probe",
    )

    def record_request(self, r: dict, model: str) -> bool:
        """Store a completed request. Returns True if it was new."""
        pick = r.get("pick") or {}
        row = {
            "task": r.get("task"),
            "slot": r.get("slot"),
            "model": model,
            "started": r.get("started"),
            "finished": r.get("finished"),
            "wall_s": r.get("wall_s"),
            "prompt_tokens": r.get("prompt_tokens"),
            "prefill_tps": r.get("prefill_tps"),
            "gen_tokens": r.get("gen_tokens"),
            "decode_tps": r.get("decode_tps"),
            "accept_rate": r.get("accept_rate"),
            "mean_len": r.get("mean_len"),
            "n_tokens": r.get("n_tokens"),
            "cache_similarity": pick.get("similarity"),
            "pick": pick.get("how"),
            "probe": 1 if is_probe(r) else 0,
        }
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO requests ({','.join(self.REQ_COLS)}) "
            f"VALUES ({','.join('?' * len(self.REQ_COLS))})",
            [row[c] for c in self.REQ_COLS],
        )
        self.conn.commit()
        return cur.rowcount > 0

    def requests_since(
        self, since: float, limit: int = 12000, until: float | None = None
    ) -> list[dict]:
        rows = self.conn.execute(
            f"SELECT {','.join(self.REQ_COLS)} FROM requests "
            f"WHERE finished >= ? AND finished <= ? ORDER BY finished DESC LIMIT ?",
            (since, until if until is not None else time.time() + 1, limit),
        ).fetchall()
        return [dict(zip(self.REQ_COLS, r)) for r in rows]

    def last_request(self, model: str, probe: bool) -> dict | None:
        row = self.conn.execute(
            f"SELECT {','.join(self.REQ_COLS)} FROM requests WHERE model=? AND probe=? "
            f"AND gen_tokens >= 24 ORDER BY finished DESC LIMIT 1",
            (model, 1 if probe else 0),
        ).fetchone()
        return dict(zip(self.REQ_COLS, row)) if row else None

    def request_stats(self, model: str, now: float) -> dict:
        """Workload versus probe, and what cold prefills cost, over 24 h."""
        rows = self.requests_since(now - 86400)
        rows = [r for r in rows if r["model"] == model]
        wl = [r for r in rows if not r["probe"]]
        pr = [r for r in rows if r["probe"]]

        def med(vals):
            vals = [v for v in vals if v is not None]
            return statistics.median(vals) if vals else None

        cold = [
            r
            for r in wl
            if (r["prompt_tokens"] or 0) >= config.COLD_PREFILL_TOKENS and r["prefill_tps"]
        ]
        out = {
            "window_s": 86400,
            "workload_n": len(wl),
            "probe_n": len(pr),
            "workload_decode_med": med(
                [r["decode_tps"] for r in wl if (r["gen_tokens"] or 0) >= 24]
            ),
            "workload_depth_med": med([r["n_tokens"] for r in wl]),
            "workload_accept_med": med([r["accept_rate"] for r in wl]),
            "probe_decode_med": med([r["decode_tps"] for r in pr]),
            "cold_n": len(cold),
            "cold_seconds": sum(r["prompt_tokens"] / r["prefill_tps"] for r in cold),
        }
        day = self.usage(now - 86400, now, 86400)["totals"]
        out["gpu_kwh_24h"] = day["gpu_wh"] / 1000 if day["covered_s"] else None
        out["host_kwh_24h"] = day["host_wh"] / 1000 if day["host_wh"] is not None else None
        for label, secs in (("1h", 3600), ("24h", 86400), ("7d", 604800)):
            row = self.conn.execute(
                "SELECT SUM(auth_fail), SUM(cache_evict), SUM(cache_skip), SUM(cancels), "
                "MIN(CASE WHEN auth_fail IS NOT NULL THEN ts END) FROM samples WHERE ts >= ?",
                (now - secs,),
            ).fetchone()
            out[f"log_{label}"] = {
                "auth_fail": row[0],
                "cache_evict": row[1],
                "cache_skip": row[2],
                "cancels": row[3],
                "counting_since": row[4],
            }
        return out

    def events_since(self, since: float, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, level, key, title, detail, state FROM events "
            "WHERE ts > ? ORDER BY ts LIMIT ?",
            (since, limit),
        ).fetchall()
        return [dict(zip(("ts", "level", "key", "title", "detail", "state"), r)) for r in rows]

    def log_event(self, ts, level, key, title, detail, state) -> None:
        self.conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?)", (ts, level, key, title, detail, state)
        )
        self.conn.commit()

    def recent_events(self, limit=60) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, level, key, title, detail, state FROM events ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(zip(("ts", "level", "key", "title", "detail", "state"), r)) for r in rows]

    def usage(self, since: float, until: float, bucket: float, tz_offset: float = 0.0) -> dict:
        """Tokens, requests and energy per bucket; see usage.usage_report()."""
        return usage_report(self, since, until, bucket, tz_offset)

    def series(self, since: float, bucket: float, until: float | None = None) -> dict:
        """Charted history for [since, until), bucketed. `until` defaults to now;
        a past one serves a frozen window opened from a link."""
        until = until if until is not None else time.time() + 1
        sfx = history_source(time.time() - since)
        if sfx and self.get_kv(f"rollup{sfx}_upto") is None:
            sfx = ""  # rollups not built yet: raw is all there is
        cols = [c for c in SAMPLE_COLS if c != "ts"]
        rows = self.conn.execute(
            f"SELECT CAST(ts/? AS INTEGER)*? AS b, {rollup_select(cols)} FROM samples{sfx} "
            f"WHERE ts >= ? AND ts < ? GROUP BY b ORDER BY b",
            (bucket, bucket, since, until),
        ).fetchall()
        out = {"ts": [r[0] for r in rows]}
        for i, c in enumerate(cols, start=1):
            out[c] = [r[i] for r in rows]

        # Peaks stay peaks through bucketing. COALESCE covers rows written
        # before the peak columns existed, whose single sample is its own max.
        grows = self.conn.execute(
            f"SELECT CAST(ts/? AS INTEGER)*? AS b, idx, {rollup_select(GPU_COLS, gpu=True)} "
            f"FROM gpu_samples{sfx} WHERE ts >= ? AND ts < ? GROUP BY b, idx ORDER BY b",
            (bucket, bucket, since, until),
        ).fetchall()
        gpus: dict[str, dict[str, list]] = {}
        for r in grows:
            idx = str(r[1])
            g = gpus.setdefault(idx, {"ts": [], **{c: [] for c in GPU_COLS}})
            g["ts"].append(r[0])
            for i, c in enumerate(GPU_COLS, start=2):
                g[c].append(r[i])
        # Past the raw request log, per-request marks give way to hourly
        # trends: count-weighted means per bucket, from requests_1h.
        req = None
        if time.time() - since > config.RETAIN_DAYS * 86400 * 1.01:
            keys = ("ts", "wl_decode", "probe_decode", "wl_prefill", "accept", "n_wl", "n_probe")
            req = [
                dict(zip(keys, r))
                for r in self.conn.execute(
                    "SELECT CAST(ts/? AS INTEGER)*? AS b, "
                    "SUM(decode_wl * decode_wl_n) / NULLIF(SUM(decode_wl_n), 0), "
                    "AVG(decode_probe), "
                    "SUM(prefill_wl * prefill_wl_n) / NULLIF(SUM(prefill_wl_n), 0), "
                    "AVG(accept_wl), "
                    "SUM(n_wl), SUM(n_probe) FROM requests_1h WHERE ts >= ? AND ts < ? "
                    "GROUP BY b ORDER BY b",
                    (bucket, bucket, since, until),
                )
            ]
        return {
            "samples": out,
            "gpus": gpus,
            "bucket": bucket,
            "source": sfx.lstrip("_") or "raw",
            "requests": req,
        }

    def rollup(self, now: float) -> None:
        """Fold complete buckets of raw history into the rollup tables.

        Incremental - a kv cursor per table records how far it has got - and
        idempotent (INSERT OR REPLACE), so the first run simply backfills all
        the raw history there is. A bucket is folded only once it can no
        longer change: two persist windows after its end.
        """
        lag = 2 * config.POLL_INTERVAL * config.PERSIST_EVERY
        cols = [c for c in SAMPLE_COLS if c != "ts"]
        for name, width in config.ROLLUPS.items():
            key = f"rollup_{name}_upto"
            upto = self.get_kv(key)
            if upto is None:
                first = self.conn.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
                if first is None:
                    continue
                upto = math.floor(first / width) * width
            until = math.floor((now - lag) / width) * width
            if until <= upto:
                continue
            b = f"CAST(ts / {width} AS INTEGER) * {width}"
            self.conn.execute(
                f"INSERT OR REPLACE INTO samples_{name} (ts, {','.join(cols)}) "
                f"SELECT {b} AS bts, {rollup_select(cols)} FROM samples "
                f"WHERE ts >= ? AND ts < ? GROUP BY bts",
                (upto, until),
            )
            self.conn.execute(
                f"INSERT OR REPLACE INTO gpu_samples_{name} (ts, idx, {','.join(GPU_COLS)}) "
                f"SELECT {b} AS bts, idx, {rollup_select(GPU_COLS, gpu=True)} FROM gpu_samples "
                f"WHERE ts >= ? AND ts < ? GROUP BY bts, idx",
                (upto, until),
            )
            if name == "1h":
                self.conn.execute(
                    "INSERT OR REPLACE INTO requests_1h "
                    "SELECT CAST(finished / 3600 AS INTEGER) * 3600 AS bts, COALESCE(model, ''), "
                    "SUM(probe = 0), SUM(probe = 1), "
                    "SUM(CASE WHEN probe = 0 THEN gen_tokens END), "
                    "SUM(CASE WHEN probe = 1 THEN gen_tokens END), "
                    "SUM(CASE WHEN probe = 0 THEN prompt_tokens END), "
                    "SUM(CASE WHEN probe = 1 THEN prompt_tokens END), "
                    "AVG(CASE WHEN probe = 0 AND gen_tokens >= 24 THEN decode_tps END), "
                    "SUM(CASE WHEN probe = 0 AND gen_tokens >= 24 AND decode_tps IS NOT NULL "
                    "    THEN 1 ELSE 0 END), "
                    "AVG(CASE WHEN probe = 1 THEN decode_tps END), "
                    "AVG(CASE WHEN probe = 0 AND prompt_tokens >= ? THEN prefill_tps END), "
                    "SUM(CASE WHEN probe = 0 AND prompt_tokens >= ? AND prefill_tps IS NOT NULL "
                    "    THEN 1 ELSE 0 END), "
                    "AVG(CASE WHEN probe = 0 THEN accept_rate END), "
                    "SUM(CASE WHEN probe = 0 AND prompt_tokens >= ? THEN 1 ELSE 0 END) "
                    "FROM requests WHERE finished >= ? AND finished < ? GROUP BY bts, model",
                    (
                        config.MIN_PREFILL_TOKENS,
                        config.MIN_PREFILL_TOKENS,
                        config.COLD_PREFILL_TOKENS,
                        upto,
                        until,
                    ),
                )
            self.set_kv(key, until)  # commits

    def prune(self) -> None:
        now = time.time()
        cutoff = now - config.RETAIN_DAYS * 86400
        for table in ("samples", "gpu_samples"):
            self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        for table, days in (
            ("samples_5m", config.ROLLUP_5M_DAYS),
            ("gpu_samples_5m", config.ROLLUP_5M_DAYS),
            ("samples_1h", config.ROLLUP_1H_DAYS),
            ("gpu_samples_1h", config.ROLLUP_1H_DAYS),
            ("requests_1h", config.ROLLUP_1H_DAYS),
            # The event log is tiny and marks long charts too.
            ("events", config.ROLLUP_1H_DAYS),
        ):
            self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (now - days * 86400,))
        self.conn.execute("DELETE FROM requests WHERE finished < ?", (cutoff,))
        # Observations are kept longer and capped by count, since the baseline
        # cares about "the last N for this model", not about wall-clock age.
        self.conn.execute(
            "DELETE FROM decode_obs WHERE rowid NOT IN "
            "(SELECT rowid FROM decode_obs ORDER BY ts DESC LIMIT 20000)"
        )
        self.conn.commit()
