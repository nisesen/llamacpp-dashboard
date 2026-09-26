"""The SQLite schema, and how a window of polls collapses into one stored row."""

from __future__ import annotations

from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts REAL PRIMARY KEY, decode_tps REAL, prefill_tps REAL,
  req_proc REAL, req_def REAL, accept_rate REAL, cache_hit REAL,
  ctx_used REAL, busy_slots REAL, cpu_pct REAL, ram_pct REAL, load1 REAL,
  net_rx REAL, net_tx REAL, cpu_temp REAL, sys_temp REAL, v12 REAL,
  tok_total REAL, prompt_total REAL, healthy INTEGER
);
CREATE TABLE IF NOT EXISTS gpu_samples (
  ts REAL, idx INTEGER, util REAL, mem_pct REAL, temp REAL,
  hbm REAL, pwr REAL, sm_clk REAL, PRIMARY KEY (ts, idx)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS events (
  ts REAL, level TEXT, key TEXT, title TEXT, detail TEXT, state TEXT
);
CREATE TABLE IF NOT EXISTS decode_obs (
  ts REAL, model TEXT, tps REAL
);
-- One row per completed request, from llama.cpp's own journal. The live
-- follower only keeps a short deque, so this is what makes the depth curve
-- and the occupancy timeline cover more than the last few minutes.
-- UNIQUE(task, finished) dedupes re-reads; task ids restart from 0 when the
-- server does, so task alone is not unique over time.
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task REAL, slot REAL, model TEXT,
  started REAL, finished REAL, wall_s REAL,
  prompt_tokens REAL, prefill_tps REAL,
  gen_tokens REAL, decode_tps REAL,
  accept_rate REAL, mean_len REAL,
  n_tokens REAL, cache_similarity REAL, pick TEXT,
  UNIQUE (task, finished)
);
CREATE INDEX IF NOT EXISTS ix_requests_ts ON requests (finished);
-- The request log folded per hour and model, kept as long as the hourly
-- rollups: long ranges plot decode/prefill trends from it after the raw
-- request rows have aged out.
CREATE TABLE IF NOT EXISTS requests_1h (
  ts REAL, model TEXT, n_wl REAL, n_probe REAL, gen_wl REAL, gen_probe REAL,
  prompt_wl REAL, prompt_probe REAL, decode_wl REAL, decode_wl_n REAL,
  decode_probe REAL, prefill_wl REAL, prefill_wl_n REAL, accept_wl REAL,
  cold_n REAL, PRIMARY KEY (ts, model)
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS ix_obs_model ON decode_obs (model, ts);
"""

SAMPLE_COLS = [
    "ts",
    "decode_tps",
    "prefill_tps",
    "req_proc",
    "req_def",
    "accept_rate",
    "cache_hit",
    "ctx_used",
    "busy_slots",
    "cpu_pct",
    "ram_pct",
    "load1",
    "net_rx",
    "net_tx",
    "cpu_temp",
    "sys_temp",
    "v12",
    "tok_total",
    "prompt_total",
    "healthy",
    "tg_live",
    "disk_read",
    "auth_fail",
    "cache_evict",
    "cache_skip",
    "cancels",
    "host_w",
]
# Per-GPU peaks alongside the means. A stored row summarises PERSIST_EVERY
# polls, and history is bucketed further for long ranges; averaging alone
# flattens a multi-day power chart to a fraction of the real peak draw.
GPU_PEAK_COLS = {"pwr_max": "pwr", "hbm_max": "hbm", "temp_max": "temp"}
GPU_COLS = ["util", "mem_pct", "temp", "hbm", "pwr", "sm_clk", "pcie_rx", "pcie_tx", *GPU_PEAK_COLS]

# How a window of polls collapses into one stored row. Anything not listed is
# averaged. Event counts add up, peaks and occupancy keep their maximum, and
# an event-sampled rate keeps the mean of the polls that carried one - taking
# only the last poll dropped four of every five completed requests.
SUM_COLS = {"auth_fail", "cache_evict", "cache_skip", "cancels"}
MAX_COLS = {"req_proc", "req_def", "busy_slots", "ctx_used", "tg_live"}
LAST_COLS = {"tok_total", "prompt_total"}
MIN_COLS = {"healthy"}


def rollup_select(cols: list[str], gpu: bool = False) -> str:
    """SQL aggregates that fold rows into one per bucket, by each column's
    rule: counts add up, occupancy and peaks keep their maximum, health keeps
    its minimum, the rest average. Used for rollups and for charting, so a
    long range reads the same whichever table it comes from."""
    parts = []
    for c in cols:
        if gpu and c in GPU_PEAK_COLS:
            parts.append(f"MAX(COALESCE({c}, {GPU_PEAK_COLS[c]})) AS {c}")
        elif not gpu and c in SUM_COLS:
            parts.append(f"SUM({c}) AS {c}")
        elif not gpu and (c in MAX_COLS or c in LAST_COLS):
            parts.append(f"MAX({c}) AS {c}")
        elif not gpu and c in MIN_COLS:
            parts.append(f"MIN({c}) AS {c}")
        else:
            parts.append(f"AVG({c}) AS {c}")
    return ", ".join(parts)


def history_source(span: float) -> str:
    """Which table suffix serves a range: raw while raw history covers it,
    then 5-minute rollups, then hourly ones."""
    if span <= config.RETAIN_DAYS * 86400 * 1.01:
        return ""
    return "_5m" if span <= config.ROLLUP_5M_DAYS * 86400 * 1.01 else "_1h"


def aggregate_window(window: list[tuple[dict, list[dict]]]) -> tuple[dict, list[dict]]:
    """Collapse [(point, gpus), ...] from consecutive polls into one row."""
    points = [p for p, _ in window]
    out: dict[str, Any] = {"ts": points[-1]["ts"]}
    for c in SAMPLE_COLS:
        if c == "ts":
            continue
        vals = [p[c] for p in points if p.get(c) is not None]
        if not vals:
            out[c] = None
        elif c in SUM_COLS:
            out[c] = sum(vals)
        elif c in MAX_COLS:
            out[c] = max(vals)
        elif c in LAST_COLS:
            out[c] = vals[-1]
        elif c in MIN_COLS:
            out[c] = min(vals)
        else:
            out[c] = sum(vals) / len(vals)

    by_idx: dict[Any, list[dict]] = {}
    for _, gpus in window:
        for g in gpus:
            by_idx.setdefault(g.get("idx"), []).append(g)
    agg_gpus = []
    for idx, rows in by_idx.items():
        g: dict[str, Any] = {"idx": idx}
        for c in GPU_COLS:
            if c in GPU_PEAK_COLS:
                vals = [r[GPU_PEAK_COLS[c]] for r in rows if r.get(GPU_PEAK_COLS[c]) is not None]
                g[c] = max(vals) if vals else None
            else:
                vals = [r[c] for r in rows if r.get(c) is not None]
                g[c] = sum(vals) / len(vals) if vals else None
        agg_gpus.append(g)
    return out, agg_gpus
