"""The poll loop: read the agent and llama.cpp every POLL_INTERVAL, derive the
snapshot, persist it, evaluate alerts and broadcast a tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
from fastapi import WebSocket

from . import config
from .alerts import AlertGate, transition_signal, worst_level
from .derive import (
    counter_deltas,
    counter_rates,
    cpu_usage,
    disk_rates,
    gpu_rows,
    host_power,
    lifetime_totals,
    live_decode,
    net_rates,
    pct,
    pick_sensor,
    pick_sensor_full,
    slot_rows,
    spec_positions,
)
from .notify import Notifier, configured_sender
from .rules import evaluate_alerts
from .schema import aggregate_window
from .snapshot import section_sigs, slim_snapshot, strip_point
from .sources import METRICS_SLEEP_SAFE_BUILD, build_number, fetch_agent, fetch_llama
from .store import Store
from .workload import is_probe, request_view

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, store: Store):
        self.store = store
        self.points: deque[dict] = deque(maxlen=config.LIVE_POINTS)
        self.snapshot: dict[str, Any] = {"booting": True}
        # Each client with the section signatures it already holds; see
        # slim_snapshot(). self.sigs always describes self.snapshot.
        self.clients: dict[WebSocket, dict[str, bytes]] = {}
        self.sigs: dict[str, bytes] = {}
        self.prev_metrics: dict[str, float] | None = None
        self.prev_ts: float | None = None
        self.prev_cpu: Any = None
        self.prev_net: dict | None = None
        self.active_alerts: dict[str, dict] = {}
        self.tick = 0
        self.started = time.time()
        self.client: httpx.AsyncClient | None = None
        self.llama_url = config.LLAMA_URL
        self.current_model: str | None = None
        self.prev_disk: dict | None = None
        self.new_requests = 0
        self.gate = AlertGate()
        # Push notifications, when NOTIFY_URLS or NOTIFY_COMMAND is set.
        sender = configured_sender()
        self.notifier = Notifier(store, sender, self.started) if sender else None
        self.notify_task: asyncio.Task | None = None
        # Polls since the last persisted row; see aggregate_window().
        self.window: list[tuple[dict, list[dict]]] = []
        self.prev_counters: dict | None = None
        # Last completed request of each kind, for the tiles. Seeded from the
        # store once the model is known, so a restart does not blank them.
        self.last_req: dict[str, dict | None] = {}
        self.last_req_model: str | None = None
        self.stats: dict = {}
        self.stats_at = 0.0
        self.last_poll_ok: float | None = None
        # The last /slots answer, standing in while /slots is not read.
        self.last_slots: list | None = None
        self.last_slots_at = 0.0
        # The agent's per-pattern journal match counts (see _read_journal).
        self.journal_matched: dict[str, int] = {}

    async def poll_once(self) -> None:
        now = time.time()
        agent = await fetch_agent(self.client) if config.AGENT_ENABLED else {}
        ct = self._follow_endpoint(agent)
        active_unit = ct.get("active_unit")
        # The serving unit's own settings, as the agent read them from its args.
        unit = ((ct.get("units") or {}).get(active_unit) or {}) if active_unit else {}
        llama = await fetch_llama(self.client, self.llama_url, unit.get("sleep_idle_s"))

        point: dict[str, Any] = {"ts": now}
        snap: dict[str, Any] = {"ts": now, "poll_interval": config.POLL_INTERVAL}
        if config.DEMO:
            snap["demo"] = True
        if not config.AGENT_ENABLED:
            snap["agent_enabled"] = False
        dt = (now - self.prev_ts) if self.prev_ts else None

        metrics, model_key = self._read_llama(llama, now, active_unit, unit, point, snap)
        gpus = self._read_host(agent, dt, point, snap)
        self._read_journal(agent, now, model_key, unit, point, snap)
        self._read_devices(agent, dt, gpus, point, snap)
        guests = agent.get("guests", {}) or {}
        snap["guests"] = guests.get("guests", [])
        snap["llm_ct"] = ct
        snap["units"] = ct.get("units", {})
        snap["host_uptime"] = agent.get("uptime")
        snap["dashboard_uptime"] = now - self.started
        self._judge(snap, point, now)
        await self._publish(snap, point, gpus, metrics, now)

    def _follow_endpoint(self, agent: dict) -> dict:
        """The target the agent found llama.cpp in. Unless LLAMA_URL says
        otherwise, its endpoint is whatever is running there, so switching
        models or moving the container needs no config change here."""
        ct = (agent.get("guests") or {}).get("llm_ct") or {}
        discovered = None if config.LLAMA_URL else ct.get("endpoint")
        if discovered and discovered != self.llama_url:
            log.info("inference endpoint -> %s", discovered)
            self.llama_url = discovered
        return ct

    # ------------------------------------------------------------ llama.cpp --

    def _read_llama(self, llama, now, active_unit, unit, point, snap):
        """Rates, slots, model identity and lifetime totals from llama.cpp.
        Returns its metrics (for the next poll's rates) and the model's key."""
        metrics, labelled = llama.get("metrics") or ({}, {})
        prev = self.prev_metrics
        if (
            prev
            and metrics
            and metrics.get("llamacpp:tokens_predicted_total", 0)
            < prev.get("llamacpp:tokens_predicted_total", 0)
        ):
            prev = None
            log.info("llama.cpp counters went backwards - server restarted")
        point.update(counter_rates(metrics, prev))
        point["healthy"] = 1 if llama.get("healthy") else 0

        props = llama.get("props") or {}
        sleeping = bool(props.get("is_sleeping"))
        slots, slots_age = self._slots(llama, sleeping, now, point)
        model_path = props.get("model_path") or ""
        model_key = model_path or props.get("model_alias") or ""
        baseline, nobs = self._track_model(model_key, now, point)
        point["decode_baseline"] = baseline

        sleep_idle_s = unit.get("sleep_idle_s")
        snap["llama"] = {
            "reachable": llama.get("reachable"),
            "healthy": llama.get("healthy"),
            "health_status": llama.get("health_status"),
            "latency_ms": llama.get("latency_ms"),
            "error": llama.get("error"),
            "url": llama.get("url"),
            "loading": llama.get("loading"),
            "sleeping": sleeping,
            "sleep_idle_s": sleep_idle_s,
            "slots_polled": bool(llama.get("slots_polled")),
            "slots_age": slots_age,
            "slots_mode": config.SLOTS_POLL,
            # Before this build a /metrics scrape itself kept the model awake.
            "sleep_blocked": bool(
                sleep_idle_s
                and sleep_idle_s > 0
                and (build_number(props.get("build_info")) or 10**9) < METRICS_SLEEP_SAFE_BUILD
            ),
        }
        snap["model"] = {
            # model_ftype misreports mixed-precision quants (a Q8_K_XL file
            # can read "Q4_K - Medium"), so model_path is the trustworthy identity.
            "alias": props.get("model_alias"),
            "path": model_path,
            "file": Path(model_path).name if model_path else None,
            "build": props.get("build_info"),
            "total_slots": props.get("total_slots"),
            "sleeping": props.get("is_sleeping"),
            "n_ctx": slots[0]["n_ctx"]
            if slots
            else (props.get("default_generation_settings") or {}).get("n_ctx"),
            "ftype_reported": props.get("model_ftype"),
            "unit": active_unit,
            # When the serving unit itself last started - not the container,
            # which had been up five days when the unit was minutes old.
            "unit_since": unit.get("active_since"),
            "baseline_tps": baseline,
            "baseline_samples": nobs,
        }
        snap["slots"] = slots
        snap["spec_positions"] = spec_positions(labelled)
        snap["totals"] = lifetime_totals(metrics)
        return metrics, model_key

    def _slots(self, llama, sleeping, now, point):
        """/slots is not read on every poll when the model may sleep (see
        SLOTS_POLL); between reads the last answer stands, with its age. A
        sleeping model has freed its slots, so nothing stands in for it."""
        raw_slots, slots_age = llama.get("slots"), None
        if llama.get("slots_polled"):
            self.last_slots, self.last_slots_at = raw_slots, now
        elif sleeping:
            raw_slots = None
        elif self.last_slots is not None:
            raw_slots, slots_age = self.last_slots, now - self.last_slots_at
        slots, point["ctx_used"] = slot_rows(raw_slots, sleeping)
        return slots, slots_age

    def _track_model(self, model_key, now, point):
        """Log a model change, and learn this model's decode baseline. Only
        observations taken while genuinely generating teach it anything."""
        if model_key and model_key != self.current_model:
            if self.current_model is not None:
                self.store.log_event(
                    now,
                    "warning",
                    "model_change",
                    "Model changed",
                    f"{Path(self.current_model).name or self.current_model}"
                    f" -> {Path(model_key).name or model_key}",
                    "raised",
                )
                log.info("model changed: %s -> %s", self.current_model, model_key)
            self.current_model = model_key
        if not model_key:
            return None, 0
        if point["decode_tps"] is not None and point.get("gen_tokens", 0) >= 24:
            self.store.record_decode(now, model_key, point["decode_tps"])
        return self.store.decode_baseline(model_key)

    # ----------------------------------------------------------------- host --

    def _read_host(self, agent, dt, point, snap) -> list[dict]:
        """GPUs, CPU, memory and network from the agent. Returns the GPU rows."""
        snap["agent_ok"] = bool(agent.get("agent_ok"))
        snap["agent_error"] = agent.get("error")
        snap["static"] = agent.get("static", {})
        snap["ages"] = agent.get("ages", {})

        gpu_block = agent.get("gpu", {}) or {}
        snap["gpu_available"] = gpu_block.get("available", False)
        gpus = gpu_rows(gpu_block, agent.get("gpu_limits", {}) or {})
        snap["gpus"] = gpus
        snap["gpu_procs"] = gpu_block.get("procs", [])
        point["gpus"] = [
            {k: g[k] for k in ("idx", "util", "mem_pct", "temp", "hbm", "pwr", "sm_clk")}
            for g in gpus
        ]

        cpu = agent.get("cpu", {}) or {}
        cpu_pct, per_core_pct = cpu_usage(cpu, self.prev_cpu)
        self.prev_cpu = cpu
        point["cpu_pct"] = cpu_pct
        point["load1"] = (cpu.get("load") or [None])[0]
        snap["cpu"] = {
            "pct": cpu_pct,
            "per_core": per_core_pct,
            "cores": cpu.get("cores"),
            "load": cpu.get("load"),
            "mhz_avg": cpu.get("mhz_avg"),
            "mhz_max": cpu.get("mhz_max"),
            "procs_running": cpu.get("procs_running"),
            "model": snap["static"].get("cpu_model"),
        }

        mem = agent.get("mem", {}) or {}
        point["ram_pct"] = pct(mem.get("used"), mem.get("total"))
        snap["mem"] = mem

        net = agent.get("net", {}) or {}
        primary, ifaces, rx_bps, tx_bps = net_rates(net, self.prev_net, dt)
        self.prev_net = net
        point["net_rx"], point["net_tx"] = rx_bps, tx_bps
        snap["net"] = {"rx_bps": rx_bps, "tx_bps": tx_bps, "primary": primary, "ifaces": ifaces}
        return gpus

    def _read_devices(self, agent, dt, gpus, point, snap) -> None:
        """PCIe traffic per card, disk I/O, and the BMC's sensors and log."""
        # PCIe: the real cross-card activation traffic.
        pcie = (agent.get("pcie") or {}).get("gpus", {}) or {}
        for g in gpus:
            p_ = pcie.get(str(g["idx"]), {})
            g["pcie_rx"] = p_.get("rx_mbs")
            g["pcie_tx"] = p_.get("tx_mbs")
        for entry, g in zip(point["gpus"], gpus):
            entry["pcie_rx"] = g.get("pcie_rx")
            entry["pcie_tx"] = g.get("pcie_tx")
        snap["pcie_available"] = (agent.get("pcie") or {}).get("available", False)

        disk = agent.get("disk_io", {}) or {}
        read_bps, write_bps = disk_rates(disk, self.prev_disk, dt)
        self.prev_disk = disk
        point["disk_read"] = read_bps
        snap["disk"] = {"read_bps": read_bps, "write_bps": write_bps, "devices": list(disk)}

        ipmi = (agent.get("ipmi") or {}).get("sensors", [])
        snap["ipmi_sensors"] = ipmi
        snap["ipmi_available"] = (agent.get("ipmi") or {}).get("available", False)
        point["cpu_temp"] = pick_sensor(ipmi, "temp", ("cpu",))
        point["sys_temp"] = pick_sensor(ipmi, "temp", ("system", "ambient", "inlet"))
        point["host_w"] = host_power(ipmi)
        rail = pick_sensor_full(ipmi, "volt", ("12v", "12 v"))
        point["v12"] = rail["value"] if rail else None
        snap["rail"] = rail

        snap["sel"] = agent.get("sel", {})
        snap["xid"] = agent.get("xid", {})
        snap["disks"] = agent.get("disks", [])

    # -------------------------------------------------------------- journal --

    def _read_journal(self, agent, now, model_key, unit, point, snap) -> None:
        """What the agent read from llama.cpp's log: completed requests, live
        slots, load state and the counters /metrics does not carry."""
        journal = agent.get("journal", {}) or {}
        # Lines matched per journal pattern: read by the request_telemetry rule
        # and exported on /metrics, but kept out of the tick the page gets.
        if "matched" in journal:
            self.journal_matched = journal["matched"]
            journal = {k: v for k, v in journal.items() if k != "matched"}
            point["parsed_timings"] = self.journal_matched.get("eval")
        snap["journal"] = journal
        snap["arch"] = agent.get("arch", {}) or {}

        self._record_requests(journal, model_key)
        snap["last"] = {k: request_view(v) for k, v in self.last_req.items()}
        if now - self.stats_at > 60 and model_key:
            try:
                self.stats = self.store.request_stats(model_key, now)
                self.stats_at = now
            except Exception as exc:
                log.warning("request stats failed: %s", exc)
        snap["stats"] = self.stats
        snap["probe_signature"] = {
            "enabled": config.PROBE_GEN_TOKENS is not None,
            "gen_tokens": config.PROBE_GEN_TOKENS,
            "max_tokens": config.PROBE_MAX_TOKENS,
            "cold_prefill_tokens": config.COLD_PREFILL_TOKENS,
            "min_prefill_tokens": config.MIN_PREFILL_TOKENS,
        }
        # The host-RAM prompt cache limit the serving unit runs with: its own
        # --cache-ram if set, else llama.cpp's default (as the agent reports).
        snap["cache_ram_mib"] = unit.get("cache_ram_mib")

        counters = journal.get("counters") or {}
        point.update(counter_deltas(counters, self.prev_counters))
        if counters:
            self.prev_counters = counters
        live_slots, point["tg_live"] = live_decode(journal)
        snap["live_slots"] = live_slots
        load = journal.get("load", {}) or {}
        snap["loading"] = load.get("state") == "loading"

    def _record_requests(self, journal, model_key) -> None:
        """Completed requests are short-lived in the follower's deque; persist
        each one exactly once so the depth curve and timeline have history."""
        if model_key and model_key != self.last_req_model:
            self.last_req_model = model_key
            self.last_req = {
                "workload": self.store.last_request(model_key, False),
                "probe": self.store.last_request(model_key, True),
            }
            self.stats_at = 0.0
        for r in sorted(journal.get("requests", []), key=lambda r: r.get("finished") or 0):
            if r.get("finished") is None:
                continue
            try:
                if self.store.record_request(r, model_key):
                    self.new_requests += 1
                    # A 5-token title request says nothing about decode speed;
                    # the tiles follow the last request that did real work.
                    if is_probe(r):
                        self.last_req["probe"] = r
                    elif (r.get("gen_tokens") or 0) >= 24:
                        self.last_req["workload"] = r
            except Exception as exc:
                log.warning("request store failed: %s", exc)

    # ------------------------------------------------ alerts and publishing --

    def _judge(self, snap, point, now) -> None:
        """Transitions, alert rules and holds; log the edges."""
        edge = self.gate.update_transition(transition_signal(snap), now)
        if edge:
            kind, info = edge
            if kind == "started":
                self.store.log_event(
                    now,
                    "info",
                    "transition",
                    "Server restarting or loading",
                    info["reason"],
                    "raised",
                )
            else:
                # Measured to the last in-progress signal, not the tail.
                self.store.log_event(
                    now,
                    "info",
                    "transition",
                    "Server restarting or loading",
                    f"back after {info['last'] - info['since']:.0f}s",
                    "cleared",
                )
        alerts = self.gate.apply(evaluate_alerts(snap, point, self.store), now)
        self.reconcile_alerts(alerts, now)
        if self.notifier:
            self.notifier.update(alerts, now)
        snap["alerts"] = alerts
        snap["worst"] = worst_level(alerts)
        snap["transition"] = self.gate.view(now)

    async def _publish(self, snap, point, gpus, metrics, now) -> None:
        """Make this poll the current state, persist every PERSIST_EVERY polls,
        and send the tick."""
        if metrics:
            self.prev_metrics, self.prev_ts = metrics, now
        elif not self.prev_ts:
            self.prev_ts = now
        self.points.append(strip_point(point))
        self.snapshot = snap
        self.sigs = section_sigs(snap)  # together: no await between
        self.tick += 1
        self.window.append((dict(point), [dict(g) for g in gpus]))
        if len(self.window) >= config.PERSIST_EVERY:
            try:
                self.store.write(*aggregate_window(self.window))
            except Exception as exc:
                log.warning("persist failed: %s", exc)
            self.window = []
        self.last_poll_ok = now
        await self.broadcast_tick(strip_point(point))

    def reconcile_alerts(self, alerts: list[dict], now: float) -> None:
        """Log only edges, so the event log reads as a history, not a firehose."""
        current = {a["key"]: a for a in alerts}
        for key, a in current.items():
            if key not in self.active_alerts:
                self.store.log_event(now, a["level"], key, a["title"], a["detail"], "raised")
        for key, a in list(self.active_alerts.items()):
            if key not in current:
                self.store.log_event(now, a["level"], key, a["title"], "", "cleared")
        self.active_alerts = current

    async def broadcast_tick(self, point: dict) -> None:
        if not self.clients:
            return
        snap_full, sigs = self.snapshot, self.sigs
        for ws, known in list(self.clients.items()):
            snap, kept = slim_snapshot(snap_full, sigs, known)
            try:
                await ws.send_text(
                    json.dumps(
                        {"type": "tick", "point": point, "snap": snap, "kept": kept}, default=str
                    )
                )
            except Exception:
                self.clients.pop(ws, None)
                continue
            if ws in self.clients:  # it may have gone while we awaited
                self.clients[ws] = sigs

    async def _deliver_notifications(self) -> None:
        """Off the poll loop: a slow destination never delays a poll."""
        while True:
            try:
                await self.notifier.deliver()
            except Exception as exc:
                log.warning("notification delivery failed: %s", exc)
            await asyncio.sleep(1.0)

    async def run(self) -> None:
        if config.DEMO:
            from .demo import replay_client

            self.client = replay_client()
        else:
            self.client = httpx.AsyncClient()
        if self.notifier:
            # Held, so the task is not garbage-collected while it runs.
            self.notify_task = asyncio.create_task(self._deliver_notifications())
        last_prune = last_rollup = 0.0
        while True:
            t0 = time.perf_counter()
            try:
                await self.poll_once()
            except Exception as exc:
                log.exception("poll failed: %s", exc)
            if time.time() - last_rollup > 300:
                last_rollup = time.time()
                try:
                    self.store.rollup(last_rollup)
                except Exception as exc:
                    log.warning("rollup failed: %s", exc)
            if time.time() - last_prune > 3600:
                last_prune = time.time()
                try:
                    self.store.prune()
                except Exception as exc:
                    log.warning("prune failed: %s", exc)
            await asyncio.sleep(max(0.05, config.POLL_INTERVAL - (time.perf_counter() - t0)))
