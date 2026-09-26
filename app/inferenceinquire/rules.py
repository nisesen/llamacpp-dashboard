"""Alert rules: a snapshot and its latest point in, a list of alerts out.

Every numeric bound here is either read from the hardware (via the agent's
per-serial limits and IPMI's own thresholds) or learned from this model's own
history. Nothing is a constant chosen for one particular GPU.

A rule is a generator `rule(snap, point, store)` that yields alerts. Rules
that need memory across polls (a baseline, a run of slow readings) keep it
in the store's key-value table. RULES runs them in order, roughly by how much
they should scare you, and that order is the order alerts are shown in.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from . import config
from .derive import pct
from .store import Store


def alert(key, level, title, detail):
    return {"key": key, "level": level, "title": title, "detail": detail}


def evaluate_alerts(snap: dict, point: dict, store: Store) -> list[dict]:
    """Every rule's alerts, in rule order."""
    return [a for rule in RULES for a in rule(snap, point, store)]


# --------------------------------------------------------------- reachability --


def reachability(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    llama = snap.get("llama", {})
    if config.AGENT_ENABLED and not snap.get("agent_ok"):
        yield alert(
            "agent",
            "serious",
            "Host agent unreachable",
            f"No telemetry from {config.AGENT_URL}. Hardware panels are stale.",
        )
    if not llama.get("reachable"):
        yield alert(
            "llama",
            "critical",
            "Inference server unreachable",
            f"{llama.get('url') or '?'} did not answer. {llama.get('error') or ''}".strip(),
        )
    elif not llama.get("healthy"):
        yield alert(
            "llama_health",
            "critical",
            "Inference server unhealthy",
            f"/health returned {llama.get('health_status')!r}.",
        )


def slow_decode(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    """The signature silent failure: active, answering HTTP 200, and running an
    order of magnitude too slow.

    Judged over consecutive OBSERVATIONS, never single polls. The llama.cpp
    counters only advance when a request finishes, so most polls carry no
    decode rate at all; those must neither extend nor break the run, or a real
    fallback would never reach the threshold. See SLOW_RUN_TO_ALERT.
    """
    d = point.get("decode_tps")
    base = point.get("decode_baseline")
    run, last_d, last_base = store.get_kv(config.SLOW_RUN_KEY) or [0, None, None]
    if d is not None and point.get("gen_tokens", 0) >= 24:
        too_slow = d < base * config.SLOW_FRACTION if base else d < config.COLD_SLOW_TPS
        run = run + 1 if too_slow else 0
        last_d, last_base = d, base
        store.set_kv(config.SLOW_RUN_KEY, [run, last_d, last_base])
    if run < config.SLOW_RUN_TO_ALERT or last_d is None:
        return
    # Held across polls that carry no observation, so a real fallback reads
    # as one sustained critical rather than a blip per completed request.
    if last_base:
        yield alert(
            "slow_decode",
            "critical",
            "Decode far below this model's norm",
            f"{last_d:.1f} tok/s against a learned median of {last_base:.1f} "
            f"for {snap.get('model', {}).get('file') or 'this model'}, "
            f"and {run} consecutive requests have been this slow. "
            f"A silent CPU fallback looks exactly like this.",
        )
    else:
        yield alert(
            "slow_decode",
            "serious",
            "Decode suspiciously slow",
            f"{last_d:.1f} tok/s over {run} consecutive requests, and no "
            f"baseline for this model yet. "
            f"A silent CPU fallback looks like this.",
        )


# ------------------------------------------------------ GPU presence and faults --


def gpu_count(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    """A card that drops off the bus: llama.cpp falls back to CPU silently."""
    if not (snap.get("agent_ok") and snap.get("gpu_available")):
        return
    gpus = snap.get("gpus", [])
    expected = store.get_kv("gpu_count_baseline", 0) or 0
    if len(gpus) > expected:
        store.set_kv("gpu_count_baseline", len(gpus))
        expected = len(gpus)
    if len(gpus) < expected:
        yield alert(
            "gpu_count",
            "critical",
            "A GPU has disappeared",
            f"{len(gpus)} of {expected} cards visible. llama.cpp "
            f"falls back to CPU without erroring.",
        )


def bmc_log(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    """A new real record in the BMC event log is news.

    The SEL is where a DIMM ECC error, a PSU fault or a power event lands.
    Baselined on first sight like the remapped rows - a board's history is not
    news - and shown for a day after a new record appears.
    """
    sel = snap.get("sel") or {}
    if not (snap.get("agent_ok") and sel.get("available") and sel.get("max_real_rid") is not None):
        return
    now = snap.get("ts") or time.time()
    rid = sel["max_real_rid"]
    seen = store.get_kv("sel_baseline")
    if seen is None:
        seen = {"rid": rid, "grew_at": None, "new": []}
        store.set_kv("sel_baseline", seen)
    elif rid > seen["rid"]:
        new = [
            e.get("what", "") for e in sel.get("entries", []) if (e.get("rid") or 0) > seen["rid"]
        ] or ["(entry not in the last 12)"]
        seen = {"rid": rid, "grew_at": now, "new": new}
        store.set_kv("sel_baseline", seen)
    elif rid < seen["rid"]:
        # The log was cleared. Re-baseline without forgetting a recent alert.
        seen = dict(seen, rid=rid)
        store.set_kv("sel_baseline", seen)
    if seen.get("grew_at") and now - seen["grew_at"] < config.SEL_ALERT_HOLD:
        new = seen.get("new") or []
        level = "serious" if any(p in w for w in new for p in config.SEL_SERIOUS) else "warning"
        yield alert(
            "sel",
            level,
            f"New BMC event: {new[-1][:90]}" if new else "New BMC event",
            f"{len(new)} new record(s) in the BMC event log"
            + (f": {'; '.join(w[:80] for w in new[-3:])}" if new else "")
            + ". Shown for 24 h.",
        )


def xid_faults(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    xid = snap.get("xid", {})
    if xid.get("count"):
        yield alert(
            "xid",
            "critical",
            f"{xid['count']} Xid fault(s) in dmesg",
            (xid.get("recent") or [""])[-1][:240],
        )


# ------------------------------------------------------------------ each card --


def gpu_cards(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    for g in snap.get("gpus", []):
        yield from _gpu_wear(g, store)
        yield from _gpu_heat(g)
        yield from _gpu_power(g, store)
        yield from _gpu_throttle(g)
        yield from _gpu_link(g)


def _gpu_wear(g: dict, store: Store) -> Iterator[dict]:
    """Remapped rows and uncorrectable ECC: news only when they grow, since a
    used card arrives with some."""
    serial = g.get("serial") or f"idx{g['idx']}"
    tag = f"GPU{g['idx']}"
    remaps = (g.get("remap_correctable") or 0) + (g.get("remap_uncorrectable") or 0)
    bkey = f"remap_baseline:{serial}"
    base_remap = store.get_kv(bkey)
    if base_remap is None:
        store.set_kv(bkey, remaps)
    elif remaps > base_remap:
        yield alert(
            f"remap:{serial}",
            "serious",
            f"{tag} remapped memory rows grew",
            f"{base_remap} -> {remaps} rows since this dashboard first saw serial {serial}.",
        )
    if g.get("remap_pending") == "Yes":
        yield alert(
            f"remap_pending:{serial}",
            "serious",
            f"{tag} has a pending row remap",
            "A reset is required to complete it.",
        )
    if g.get("remap_failure") == "Yes":
        yield alert(
            f"remap_fail:{serial}",
            "critical",
            f"{tag} row remapping FAILED",
            "The card needs replacing.",
        )

    ecc = g.get("ecc_uncorrected")
    if ecc is not None:
        ekey = f"ecc_baseline:{serial}"
        ebase = store.get_kv(ekey)
        if ebase is None:
            store.set_kv(ekey, ecc)
        elif ecc > ebase:
            yield alert(
                f"ecc:{serial}",
                "serious",
                f"{tag} uncorrectable ECC errors grew",
                f"{ebase} -> {ecc} aggregate.",
            )


def _gpu_heat(g: dict) -> Iterator[dict]:
    """Memory and core temperature, against the ceilings the card reports."""
    idx, tag, lim = g["idx"], f"GPU{g['idx']}", g.get("limits") or {}
    hbm, warn, crit = g.get("hbm"), lim.get("temp_mem_warn"), lim.get("temp_mem_crit")
    if hbm is not None and crit and hbm >= crit:
        yield alert(
            f"hbm:{idx}",
            "critical",
            f"{tag} memory at {hbm:.0f} C",
            f"The card reports {crit:.0f} C as its memory ceiling.",
        )
    elif hbm is not None and warn and hbm >= warn:
        yield alert(
            f"hbm:{idx}",
            "warning",
            f"{tag} memory at {hbm:.0f} C",
            f"Warning point {warn:.0f} C, shutdown {lim.get('temp_shutdown', '?')} C.",
        )

    core, core_warn = g.get("temp"), lim.get("temp_core_warn")
    if core is not None and core_warn and core >= core_warn:
        level = "serious" if core >= (lim.get("temp_slowdown") or 1e9) else "warning"
        yield alert(
            f"gtemp:{idx}",
            level,
            f"{tag} core at {core:.0f} C",
            f"Slowdown at {lim.get('temp_slowdown', '?')} C.",
        )


def _gpu_power(g: dict, store: Store) -> Iterator[dict]:
    """Draw over the card's own cap, judged over consecutive polls: a single
    reading over the cap is a prefill spike, not news. See POWER_RUN_TO_ALERT."""
    idx, tag, lim = g["idx"], f"GPU{g['idx']}", g.get("limits") or {}
    pwr = g.get("pwr")
    cap = g.get("pwr_limit") or lim.get("power_limit")
    if pwr is None or not cap:
        return
    runs = store.get_kv(config.POWER_RUN_KEY) or {}
    run, peak = runs.get(str(idx)) or [0, 0.0]
    if pwr >= cap * config.POWER_WARN_RATIO:
        run, peak = int(run) + 1, float(max(peak, pwr))
    else:
        run, peak = 0, 0.0
    if runs.get(str(idx)) != [run, peak]:
        runs[str(idx)] = [run, peak]
        store.set_kv(config.POWER_RUN_KEY, runs)
    if run < config.POWER_RUN_TO_ALERT:
        return
    held = run * config.POLL_INTERVAL
    # Level follows the PEAK of the run, so a spike into 'serious' is still
    # reported as such once the draw settles back.
    if peak >= cap * config.POWER_SERIOUS_RATIO:
        yield alert(
            f"pwr:{idx}",
            "serious",
            f"{tag} drawing {pwr:.0f} W",
            f"Well over its {cap:.0f} W cap for {held:.0f}s, peaking at {peak:.0f} W.",
        )
    else:
        yield alert(
            f"pwr:{idx}",
            "warning",
            f"{tag} drawing {pwr:.0f} W",
            f"Above its {cap:.0f} W cap for {held:.0f}s, peaking at "
            f"{peak:.0f} W. Brief prefill spikes are normal; a draw "
            f"that holds is not.",
        )


def _gpu_throttle(g: dict) -> Iterator[dict]:
    idx, tag = g["idx"], f"GPU{g['idx']}"
    for reason in g.get("throttle", []):
        if reason in ("hw thermal", "sw thermal"):
            yield alert(
                f"throttle:{idx}:{reason}",
                "serious",
                f"{tag} thermal throttling ({reason})",
                "",
            )
        elif reason in ("hw slowdown", "power brake"):
            yield alert(
                f"throttle:{idx}:{reason}",
                "serious",
                f"{tag} hardware slowdown ({reason})",
                "Usually a power-delivery fault.",
            )


def _gpu_link(g: dict) -> Iterator[dict]:
    """A PCIe link trained below its maximum, while the card is working (an
    idle card downshifts its link on purpose)."""
    if (
        g.get("pcie_gen")
        and g.get("pcie_gen_max")
        and g["pcie_gen"] < g["pcie_gen_max"]
        and (g.get("util") or 0) > 5
    ):
        yield alert(
            f"pcie:{g['idx']}",
            "warning",
            f"GPU{g['idx']} link at Gen{g['pcie_gen']} of Gen{g['pcie_gen_max']}",
            "",
        )


# ---------------------------------------------------------- inference service --


def inference_units(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    """Exactly one llama* unit running, and none of them crash-restarting."""
    units = snap.get("units", {})
    active = [n for n, u in units.items() if u.get("active") == "active"]
    if units and not active:
        yield alert(
            "units",
            "critical",
            "No inference unit is running",
            f"None of {', '.join(sorted(units))} is active.",
        )
    if len(active) > 1:
        yield alert(
            "units_both",
            "critical",
            "Two models are running at once",
            f"{', '.join(active)} - they will fight over VRAM.",
        )
    for name, u in units.items():
        n = u.get("restarts")
        if n is None:
            continue
        rkey = f"restarts:{name}"
        prev = store.get_kv(rkey)
        if prev is None:
            store.set_kv(rkey, n)
        elif n > prev:
            store.set_kv(rkey, n)
            yield alert(
                f"restart:{name}",
                "warning",
                f"{name} restarted",
                f"Restart count {prev} -> {n}.",
            )


# -------------------------------------------------------------- chassis, host --


def chassis_sensors(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    """IPMI sensors: every bound here is the sensor's own."""
    for s in snap.get("ipmi_sensors", []):
        if s.get("state") not in ("ok", "na", "nr", ""):
            yield alert(
                f"ipmi:{s['name']}",
                "warning",
                f"IPMI {s['name']} is {s['state']}",
                f"{s['value']} {s['unit']}",
            )
            continue
        th = s.get("thresholds") or {}
        lo, hi = th.get("lcr"), th.get("ucr")
        if lo is not None and s["value"] < lo:
            yield alert(
                f"ipmi_lo:{s['name']}",
                "serious",
                f"{s['name']} below its critical low",
                f"{s['value']} {s['unit']} against {lo}.",
            )
        elif hi is not None and s["value"] > hi:
            yield alert(
                f"ipmi_hi:{s['name']}",
                "serious",
                f"{s['name']} above its critical high",
                f"{s['value']} {s['unit']} against {hi}.",
            )


def host_resources(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    ram = point.get("ram_pct")
    if ram is not None and ram >= 93:
        yield alert("ram", "warning", f"Host memory {ram:.0f}% used", "")
    for disk in snap.get("disks", []):
        used = pct(disk.get("used"), disk.get("total"))
        if used is not None and used >= 90:
            yield alert(
                f"disk:{disk['name']}",
                "warning",
                f"Storage '{disk['name']}' {used:.0f}% full",
                "",
            )


# How each runtime's target reads in the two alerts below, as (the title when
# it cannot be probed, its detail, the title and detail when nothing serves).
# An agent older than the runtimes reports none: that is Proxmox.
TARGET_WORDS = {
    "proxmox-lxc": (
        "Cannot probe CT {vmid}",
        "pct exec into the inference container failed.",
        "No inference container found",
        "No running container exposes a llama* unit.",
    ),
    "docker": (
        "Cannot read Docker",
        "The Docker Engine API did not answer on its socket.",
        "No llama.cpp container found",
        "No container runs llama-server or matches LLM_UNIT_GLOB. If llama-server runs "
        "under systemd, run the agent on the host instead (deploy/install-agent.sh).",
    ),
    "systemd": (
        "Cannot read systemd",
        "systemctl did not answer.",
        "No llama.cpp unit found",
        "No unit on this host matches LLM_UNIT_GLOB.",
    ),
}


def inference_container(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    ct = snap.get("llm_ct", {})
    runtime = ct.get("runtime", "proxmox-lxc")
    if runtime not in TARGET_WORDS:
        return  # "none": the agent was told llama.cpp does not run on its host
    probe_title, probe_detail, missing_title, missing_detail = TARGET_WORDS[runtime]
    if ct.get("vmid") and not ct.get("reachable"):
        yield alert("ct", "serious", probe_title.format(vmid=ct["vmid"]), probe_detail)
    if snap.get("agent_ok") and not ct.get("vmid"):
        yield alert("ct_missing", "serious", missing_title, missing_detail)


def request_telemetry(snap: dict, point: dict, store: Store) -> Iterator[dict]:
    """Requests finishing in /metrics but not in the log.

    llama.cpp's tokens_predicted_total advances when a request finishes, and
    the agent counts the timing lines it parses. A build that changes its log
    format keeps the first moving and stops the second, which would otherwise
    empty the request panels without a word.
    """
    ev, tok = point.get("parsed_timings"), point.get("tok_total")
    if ev is None or tok is None:
        return  # an agent too old to count, or no /metrics this poll
    st = store.get_kv(config.PARSE_WATCH_KEY) or {}
    misses = st.get("misses", 0)
    if st.get("ev") is not None and ev != st["ev"]:
        misses = 0  # timings arrived (or the agent restarted and counts afresh)
    elif st.get("tok") is not None and tok > st["tok"]:
        misses += 1
    new = {"tok": tok, "ev": ev, "misses": misses}
    if new != st:
        store.set_kv(config.PARSE_WATCH_KEY, new)
    if misses < config.PARSE_MISSES_TO_ALERT:
        return
    journal = snap.get("journal") or {}
    yield alert(
        "journal_parse",
        "warning",
        "Request telemetry degraded - log format changed?",
        f"llama.cpp finished {misses} requests in a row that the agent found no timings for "
        f"in the {journal.get('following') or 'server'} log"
        + ("" if journal.get("alive", True) else " (and the log is not being followed)")
        + f". Build {(snap.get('model') or {}).get('build') or 'unknown'}. The request, "
        "latency and slot panels are incomplete until the agent's patterns match again.",
    )


RULES = (
    reachability,
    slow_decode,
    gpu_count,
    bmc_log,
    xid_faults,
    gpu_cards,
    inference_units,
    chassis_sensors,
    host_resources,
    inference_container,
    request_telemetry,
)
