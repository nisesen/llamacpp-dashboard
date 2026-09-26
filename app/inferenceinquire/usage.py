"""Energy and usage: tokens, requests and energy per bucket, for the usage card."""

from __future__ import annotations

import math
import statistics

from . import config
from .schema import history_source


def usage_report(store, since: float, until: float, bucket: float, tz_offset: float = 0.0) -> dict:
    """Tokens, requests and energy per bucket, and their totals.

    Tokens and requests come from the request log, so workload and probe
    stay apart. Energy integrates each stored power mean over the window
    it summarises - the time since that card's previous row - capped at
    three nominal windows, so a gap in the data (the dashboard was down)
    reads as missing rather than as hours at the last-seen draw.
    Buckets are aligned to local time via tz_offset (seconds east of UTC),
    so a daily bucket is a calendar day where the reader is.
    """
    # Long ranges read the rollups: each row then summarises its bucket,
    # so the nominal window (and the gap cap) is the bucket width.
    sfx = history_source(until - since)
    if sfx and store.get_kv(f"rollup{sfx}_upto") is None:
        sfx = ""
    nominal = config.ROLLUPS[sfx[1:]] if sfx else config.POLL_INTERVAL * config.PERSIST_EVERY
    cap = 3 * nominal

    def key(ts):
        return math.floor((ts + tz_offset) / bucket) * bucket - tz_offset

    buckets: dict[float, dict] = {}

    def slot(ts):
        k = key(ts)
        if k not in buckets:
            buckets[k] = {
                "ts": k,
                "gen_wl": 0,
                "gen_probe": 0,
                "prompt_wl": 0,
                "prompt_probe": 0,
                "req_wl": 0,
                "req_probe": 0,
                "gpu_wh": 0.0,
                "host_wh": None,
            }
        return buckets[k]

    k = key(since)
    while k < until:  # empty buckets still show as zero
        slot(k)
        k += bucket
    if sfx:
        for ts, *vals in store.conn.execute(
            "SELECT ts, n_wl, n_probe, gen_wl, gen_probe, prompt_wl, prompt_probe "
            "FROM requests_1h WHERE ts >= ? AND ts < ?",
            (since, until),
        ):
            b = slot(ts)
            for k, v in zip(
                ("req_wl", "req_probe", "gen_wl", "gen_probe", "prompt_wl", "prompt_probe"),
                vals,
            ):
                b[k] += v or 0
    else:
        for fin, gen, prompt, probe in store.conn.execute(
            "SELECT finished, gen_tokens, prompt_tokens, probe FROM requests "
            "WHERE finished >= ? AND finished < ?",
            (since, until),
        ):
            b, kind = slot(fin), ("probe" if probe else "wl")
            b[f"gen_{kind}"] += gen or 0
            b[f"prompt_{kind}"] += prompt or 0
            b[f"req_{kind}"] += 1

    # Total GPU draw per stored row, with whether a request was running.
    per_ts: dict[float, list] = {}  # ts -> [watts, dt, busy]
    for ts, pwr, dt, busy in store.conn.execute(
        "SELECT g.ts, g.pwr, g.ts - LAG(g.ts) OVER (PARTITION BY g.idx ORDER BY g.ts), "
        "COALESCE(s.req_proc, 0) > 0 "
        f"FROM gpu_samples{sfx} g LEFT JOIN samples{sfx} s ON s.ts = g.ts "
        "WHERE g.ts >= ? AND g.ts < ?",
        (since - cap, until),
    ):
        if ts < since or pwr is None or dt is None or dt <= 0:
            continue
        row = per_ts.setdefault(ts, [0.0, min(dt, cap), bool(busy)])
        row[0] += pwr
    gpu_wh = busy_wh = busy_s = covered_s = 0.0
    idle_w = []
    for ts, (w, dt, busy) in per_ts.items():
        wh = w * dt / 3600
        gpu_wh += wh
        covered_s += dt
        slot(ts)["gpu_wh"] += wh
        if busy:
            busy_wh += wh
            busy_s += dt
        else:
            idle_w.append(w)

    # The whole host, where the BMC reports its draw (see host_power()).
    host_wh = None
    for ts, w, dt in store.conn.execute(
        f"SELECT ts, host_w, ts - LAG(ts) OVER (ORDER BY ts) FROM samples{sfx} "
        "WHERE ts >= ? AND ts < ? AND host_w IS NOT NULL",
        (since - cap, until),
    ):
        if ts < since or dt is None or dt <= 0:
            continue
        wh = w * min(dt, cap) / 3600
        host_wh = (host_wh or 0.0) + wh
        b = slot(ts)
        b["host_wh"] = (b["host_wh"] or 0.0) + wh

    rows = [buckets[k] for k in sorted(buckets) if since - bucket < k < until]
    gen = sum(r["gen_wl"] + r["gen_probe"] for r in rows)
    totals = {
        "gen_wl": sum(r["gen_wl"] for r in rows),
        "gen_probe": sum(r["gen_probe"] for r in rows),
        "prompt_wl": sum(r["prompt_wl"] for r in rows),
        "prompt_probe": sum(r["prompt_probe"] for r in rows),
        "req_wl": sum(r["req_wl"] for r in rows),
        "req_probe": sum(r["req_probe"] for r in rows),
        "gpu_wh": gpu_wh,
        "host_wh": host_wh,
        "busy_wh": busy_wh,
        "covered_s": covered_s,
        "busy_share": busy_s / covered_s if covered_s else None,
        "idle_w": statistics.median(idle_w) if idle_w else None,
        # What a generated token really cost, idle included - and what it
        # cost while the GPUs were actually working.
        "wh_per_1k_gen": gpu_wh / (gen / 1000) if gen else None,
        "wh_per_1k_gen_busy": busy_wh / (gen / 1000) if gen else None,
        "price_per_kwh": config.ENERGY_PRICE_PER_KWH,
        "currency": config.ENERGY_CURRENCY,
    }
    if config.ENERGY_PRICE_PER_KWH and covered_s:
        kwh = (host_wh if host_wh is not None else gpu_wh) / 1000
        totals["cost"] = kwh * config.ENERGY_PRICE_PER_KWH
        totals["cost_per_day"] = totals["cost"] / (covered_s / 86400)
        totals["cost_per_1m_gen"] = totals["cost"] / (gen / 1e6) if gen else None
        totals["cost_basis"] = "host" if host_wh is not None else "gpu"
    return {
        "since": since,
        "until": until,
        "bucket": bucket,
        "buckets": rows,
        "totals": totals,
        "source": sfx.lstrip("_") or "raw",
    }
