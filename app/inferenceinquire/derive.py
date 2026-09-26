"""Pure functions from polled payloads to the numbers the page shows.

Nothing here does I/O or keeps state: the Monitor passes in the previous
reading where a rate needs one. That keeps the arithmetic testable on its own.

**Counters, not rates.** llama.cpp exposes cumulative counters, so live decode
and prefill tok/s, speculative acceptance and cache hit rate are differences
between two polls.
"""

from __future__ import annotations


def rate(delta_num: float, delta_den: float | None, min_den: float = 1e-3) -> float | None:
    """A rate, or None when the denominator says nothing happened."""
    if delta_den is None or delta_den <= min_den:
        return None
    return delta_num / delta_den


def pct(used: float | None, total: float | None) -> float | None:
    if not total:
        return None
    return round(100.0 * (used or 0) / total, 2)


def pick_sensor_full(sensors: list[dict], kind: str, patterns: tuple[str, ...]):
    """First sensor of a kind whose name matches - names differ per board."""
    for s in sensors:
        if s.get("kind") != kind:
            continue
        name = (s.get("name") or "").lower()
        if any(p in name for p in patterns):
            return s
    return None


def host_power(sensors: list[dict]) -> float | None:
    """The whole machine's draw, where the BMC reports it; boards name it
    differently. A single total ("Pwr Consumption", "Total Power") wins;
    otherwise per-supply input readings add up."""
    watts = [s for s in sensors if s.get("kind") == "watt" and s.get("value") is not None]
    for s in watts:
        name = (s.get("name") or "").lower()
        if any(p in name for p in ("consumption", "total", "system", "chassis")):
            return s["value"]
    psus = [
        s["value"]
        for s in watts
        if any(p in (s.get("name") or "").lower() for p in ("psu", "ps1", "ps2", "input"))
    ]
    return sum(psus) if psus else None


def pick_sensor(sensors, kind, patterns):
    s = pick_sensor_full(sensors, kind, patterns)
    return s["value"] if s else None


# ---------------------------------------------------------------- llama.cpp --


def counter_rates(metrics: dict, prev: dict | None) -> dict:
    """This poll's rates from llama.cpp's cumulative counters, plus the gauges.

    `prev` is the previous poll's metrics, or None on the first poll and after
    a restart, when there is nothing to difference against.
    """

    def delta(name: str) -> float | None:
        if not prev or name not in metrics or name not in prev:
            return None
        return metrics[name] - prev[name]

    d_pred = delta("llamacpp:tokens_predicted_total")
    d_pred_s = delta("llamacpp:tokens_predicted_seconds_total")
    d_prompt = delta("llamacpp:prompt_tokens_total")
    d_prompt_s = delta("llamacpp:prompt_seconds_total")
    d_cached = delta("llamacpp:prompt_tokens_cached_total")
    d_draft = delta("llamacpp:spec_decode_num_draft_tokens_total")
    d_acc = delta("llamacpp:spec_decode_num_accepted_tokens_total")
    d_drafts = delta("llamacpp:spec_decode_num_drafts_total")

    point: dict = {}
    point["decode_tps"] = rate(d_pred or 0, d_pred_s)
    point["prefill_tps"] = rate(d_prompt or 0, d_prompt_s)
    point["gen_tokens"] = d_pred or 0
    point["prompt_tokens_delta"] = d_prompt or 0
    point["accept_rate"] = rate(d_acc or 0, d_draft or 0, min_den=0.5)
    point["accepted_per_draft"] = rate(d_acc or 0, d_drafts or 0, min_den=0.5)
    if d_cached is not None and d_prompt is not None and (d_cached + d_prompt) > 0:
        point["cache_hit"] = d_cached / (d_cached + d_prompt)
    else:
        point["cache_hit"] = None
    point["req_proc"] = metrics.get("llamacpp:requests_processing")
    point["req_def"] = metrics.get("llamacpp:requests_deferred")
    point["busy_slots"] = metrics.get("llamacpp:n_busy_slots_per_decode")
    point["tok_total"] = metrics.get("llamacpp:tokens_predicted_total")
    point["prompt_total"] = metrics.get("llamacpp:prompt_tokens_total")
    return point


def lifetime_totals(metrics: dict) -> dict:
    """Since-start figures, from the `_total` counters.

    Not from llama.cpp's `predicted_tokens_seconds` gauge: that is not a
    lifetime average, since llama.cpp resets its bucket on every scrape.
    """
    total_pred_s = metrics.get("llamacpp:tokens_predicted_seconds_total", 0.0)
    total_prompt_s = metrics.get("llamacpp:prompt_seconds_total", 0.0)
    cum_dec = (
        metrics.get("llamacpp:tokens_predicted_total", 0.0) / total_pred_s if total_pred_s else None
    )
    cum_pre = (
        metrics.get("llamacpp:prompt_tokens_total", 0.0) / total_prompt_s
        if total_prompt_s
        else None
    )
    total_draft = metrics.get("llamacpp:spec_decode_num_draft_tokens_total", 0)
    total_acc = metrics.get("llamacpp:spec_decode_num_accepted_tokens_total", 0)
    total_drafts = metrics.get("llamacpp:spec_decode_num_drafts_total", 0)
    total_prompt = metrics.get("llamacpp:prompt_tokens_total", 0)
    total_cached = metrics.get("llamacpp:prompt_tokens_cached_total", 0)
    return {
        "tokens_predicted": metrics.get("llamacpp:tokens_predicted_total"),
        "prompt_tokens": total_prompt,
        "prompt_cached": total_cached,
        "decode_calls": metrics.get("llamacpp:n_decode_total"),
        "tokens_max": metrics.get("llamacpp:n_tokens_max"),
        "decode_tps_avg": cum_dec,
        "prefill_tps_avg": cum_pre,
        "accept_rate_avg": (total_acc / total_draft) if total_draft else None,
        "accepted_per_draft_avg": (total_acc / total_drafts) if total_drafts else None,
        "cache_hit_avg": (total_cached / (total_cached + total_prompt))
        if (total_cached + total_prompt)
        else None,
        "draft_tokens": total_draft,
        "accepted_tokens": total_acc,
        "drafts": total_drafts,
        "generating_seconds": total_pred_s,
        "prefill_seconds": total_prompt_s,
    }


def spec_positions(labelled: dict) -> list[dict]:
    """Accepted draft tokens by position in the draft."""
    positions = labelled.get("llamacpp:spec_decode_num_accepted_tokens_per_pos_total", {})
    return sorted(
        ({"position": int(k), "accepted": v} for k, v in positions.items()),
        key=lambda x: x["position"],
    )


def slot_rows(raw_slots: list | None, sleeping: bool) -> tuple[list[dict], int | None]:
    """The slots as the page shows them, and the deepest context in use.

    A sleeping model has freed its slots, so its context in use is unknown
    (None) rather than zero.
    """
    slots, ctx_used = [], (None if sleeping else 0)
    for s in raw_slots or []:
        if not isinstance(s, dict):
            continue
        nxt = s.get("next_token")
        if isinstance(nxt, list):
            nxt = nxt[0] if nxt else {}
        nxt = nxt or {}
        used = s.get("n_prompt_tokens") or 0
        ctx_used = max(ctx_used, used)
        slots.append(
            {
                "id": s.get("id"),
                "processing": bool(s.get("is_processing")),
                "n_ctx": s.get("n_ctx"),
                "prompt_tokens": used,
                "prompt_cached": s.get("n_prompt_tokens_cache"),
                "decoded": nxt.get("n_decoded"),
                "speculative": s.get("speculative"),
            }
        )
    return slots, ctx_used


# --------------------------------------------------------------------- host --


def gpu_rows(gpu_block: dict, gpu_limits: dict) -> list[dict]:
    """One row per card from the agent's nvidia-smi fields, in index order,
    each with the limits the card itself reports (keyed by serial)."""
    gpus = []
    for g in gpu_block.get("gpus", []):
        mem_used, mem_total = g.get("memory.used"), g.get("memory.total")
        serial = g.get("serial")
        gpus.append(
            {
                "idx": g.get("index"),
                "name": g.get("name"),
                "serial": serial,
                "bus": g.get("pci.bus_id"),
                "util": g.get("utilization.gpu"),
                "mem_util": g.get("utilization.memory"),
                "mem_used": mem_used,
                "mem_total": mem_total,
                "mem_pct": pct(mem_used, mem_total),
                "temp": g.get("temperature.gpu"),
                "hbm": g.get("temperature.memory"),
                "pwr": g.get("power.draw"),
                "pwr_limit": g.get("enforced.power.limit") or g.get("power.limit"),
                "sm_clk": g.get("clocks.sm"),
                "mem_clk": g.get("clocks.mem"),
                "sm_clk_max": g.get("clocks.max.sm"),
                "pstate": g.get("pstate"),
                "pcie_gen": g.get("pcie.link.gen.current"),
                "pcie_gen_max": g.get("pcie.link.gen.max"),
                "pcie_width": g.get("pcie.link.width.current"),
                "pcie_width_max": g.get("pcie.link.width.max"),
                "ecc_corrected": g.get("ecc.errors.corrected.aggregate.total"),
                "ecc_uncorrected": g.get("ecc.errors.uncorrected.aggregate.total"),
                "remap_correctable": g.get("remapped_rows.correctable"),
                "remap_uncorrectable": g.get("remapped_rows.uncorrectable"),
                "remap_pending": g.get("remapped_rows.pending"),
                "remap_failure": g.get("remapped_rows.failure"),
                "throttle": g.get("throttle", []),
                "persistence": g.get("persistence_mode"),
                "limits": gpu_limits.get(serial or "", {}),
            }
        )
    gpus.sort(key=lambda g: g["idx"] if g["idx"] is not None else 99)
    return gpus


def cpu_usage(cpu: dict, prev_cpu: dict | None) -> tuple[float | None, list]:
    """Busy percentage overall and per core, from two readings of /proc/stat."""
    if not (cpu.get("total") and prev_cpu and prev_cpu.get("total")):
        return None, []

    def busy_pct(cur, old):
        dtot, dbusy = cur[0] - old[0], cur[1] - old[1]
        return round(100.0 * dbusy / dtot, 1) if dtot > 0 else None

    return busy_pct(cpu["total"], prev_cpu["total"]), [
        busy_pct(c, o) for c, o in zip(cpu["per_core"], prev_cpu.get("per_core", []))
    ]


def net_rates(net: dict, prev_net: dict | None, dt: float | None):
    """Bytes per second on the primary interface, whichever one that is.

    Returns (primary, interface names, rx, tx). A counter that went backwards
    (an interface reset) gives no rate rather than a negative one.
    """
    ifaces = net.get("ifaces", {})
    primary = net.get("primary") or (next(iter(ifaces), None))
    rx_bps = tx_bps = None
    prev_ifaces = (prev_net or {}).get("ifaces", {})
    if dt and primary in ifaces and primary in prev_ifaces:
        d_rx = ifaces[primary]["rx"] - prev_ifaces[primary]["rx"]
        d_tx = ifaces[primary]["tx"] - prev_ifaces[primary]["tx"]
        if d_rx >= 0 and d_tx >= 0:
            rx_bps, tx_bps = d_rx / dt, d_tx / dt
    return primary, list(ifaces), rx_bps, tx_bps


def disk_rates(disk: dict, prev_disk: dict | None, dt: float | None):
    """Read and write bytes per second across all disks: what makes a model
    load visible."""
    read_bps = write_bps = None
    if prev_disk and dt:
        d_r = sum(v["read_sectors"] for v in disk.values()) - sum(
            v["read_sectors"] for v in prev_disk.values() if v
        )
        d_w = sum(v["write_sectors"] for v in disk.values()) - sum(
            v["write_sectors"] for v in prev_disk.values() if v
        )
        if d_r >= 0:
            read_bps = d_r * 512 / dt
        if d_w >= 0:
            write_bps = d_w * 512 / dt
    return read_bps, write_bps


# ------------------------------------------------------------------ journal --

# The agent's journal counters, and the stored columns they feed.
JOURNAL_COUNTERS = (
    ("auth_fail", "auth_fail"),
    ("cache_evict", "cache_evict"),
    ("cache_skip", "cache_skip"),
    ("cancel", "cancels"),
)


def counter_deltas(counters: dict, prev: dict | None) -> dict:
    """Log signals llama.cpp does not expose in /metrics, differenced from the
    agent's counters. A counter that went backwards is an agent restart: what
    it has counted since is all new."""
    out = {}
    for key, col in JOURNAL_COUNTERS:
        cur = counters.get(key)
        old = (prev or {}).get(key)
        if cur is None or prev is None:
            out[col] = None if cur is None else 0
        else:
            out[col] = cur - old if old is not None and cur >= old else cur
    return out


def live_decode(journal: dict) -> tuple[list[dict], float | None]:
    """Slots generating right now, and the fastest one's rate.

    tg_3s is llama.cpp's own 3-second windowed rate, emitted per slot while
    generating: far denser than differencing /metrics counters.
    """
    live = [
        s for s in journal.get("slots", []) if s.get("phase") == "decode" and not s.get("stale")
    ]
    return live, max((s.get("tg_3s") or 0 for s in live), default=None) or None
