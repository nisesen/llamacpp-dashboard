"""NVIDIA GPUs: nvidia-smi's query stream, per-card limits, processes, PCIe and Xid."""

import subprocess
import threading
import time
import xml.etree.ElementTree as ET

from ..cache import CACHES
from ..shell import NA_VALUES, num, run, xml_num

GPU_FIELDS = [
    "index",
    "name",
    "serial",
    "uuid",
    "pci.bus_id",
    "temperature.gpu",
    "temperature.memory",
    "utilization.gpu",
    "utilization.memory",
    "memory.used",
    "memory.total",
    "power.draw",
    "power.limit",
    "enforced.power.limit",
    "clocks.sm",
    "clocks.mem",
    "clocks.max.sm",
    "pstate",
    "pcie.link.gen.current",
    "pcie.link.gen.max",
    "pcie.link.width.current",
    "pcie.link.width.max",
    "ecc.errors.corrected.aggregate.total",
    "ecc.errors.uncorrected.aggregate.total",
    "remapped_rows.correctable",
    "remapped_rows.uncorrectable",
    "remapped_rows.pending",
    "remapped_rows.failure",
    "clocks_throttle_reasons.sw_power_cap",
    "clocks_throttle_reasons.hw_slowdown",
    "clocks_throttle_reasons.hw_thermal_slowdown",
    "clocks_throttle_reasons.sw_thermal_slowdown",
    "clocks_throttle_reasons.hw_power_brake_slowdown",
    "persistence_mode",
    "fan.speed",
]
NUMERIC_GPU_FIELDS = {
    "index",
    "temperature.gpu",
    "temperature.memory",
    "utilization.gpu",
    "utilization.memory",
    "memory.used",
    "memory.total",
    "power.draw",
    "power.limit",
    "enforced.power.limit",
    "clocks.sm",
    "clocks.mem",
    "clocks.max.sm",
    "pcie.link.gen.current",
    "pcie.link.gen.max",
    "pcie.link.width.current",
    "pcie.link.width.max",
    "ecc.errors.corrected.aggregate.total",
    "ecc.errors.uncorrected.aggregate.total",
    "remapped_rows.correctable",
    "remapped_rows.uncorrectable",
    "fan.speed",
}

THROTTLE_LABELS = (
    ("clocks_throttle_reasons.sw_power_cap", "power cap"),
    ("clocks_throttle_reasons.hw_slowdown", "hw slowdown"),
    ("clocks_throttle_reasons.hw_thermal_slowdown", "hw thermal"),
    ("clocks_throttle_reasons.sw_thermal_slowdown", "sw thermal"),
    ("clocks_throttle_reasons.hw_power_brake_slowdown", "power brake"),
)


def collect_gpu_limits():
    """Per-card limits, read from the cards themselves.

    `nvidia-smi -q -x` is structured, so this needs no extra package on the
    hypervisor. Everything the alert rules used to hardcode - the 250 W cap,
    the temperature ceilings - comes from here instead, keyed by serial, so
    replacing a card moves its thresholds with it.
    """
    out = run(["nvidia-smi", "-q", "-x"], timeout=20)
    if not out:
        return {}
    try:
        root = ET.fromstring(out)
    except ET.ParseError:
        return {}

    limits = {}
    for gpu in root.findall("gpu"):
        serial = (gpu.findtext("serial") or "").strip()
        if not serial:
            continue
        temp = gpu.find("temperature")
        power = gpu.find("gpu_power_readings")
        if power is None:
            power = gpu.find("power_readings")

        shutdown = xml_num(temp, "gpu_temp_max_threshold")
        slowdown = xml_num(temp, "gpu_temp_slow_threshold")
        mem_max = xml_num(temp, "gpu_temp_max_mem_threshold")

        # Some cards (A100s among them) report a shutdown point but leave slowdown and the
        # memory ceiling as N/A, so derive from what IS reported rather than
        # substituting a number for this particular model of card.
        if slowdown is None and shutdown is not None:
            slowdown = shutdown - 10
        core_warn = slowdown - 10 if slowdown is not None else None
        mem_warn = (mem_max - 10) if mem_max else (shutdown - 15 if shutdown else None)
        mem_crit = mem_max or (shutdown - 5 if shutdown else None)

        limits[serial] = {
            "temp_shutdown": shutdown,
            "temp_slowdown": slowdown,
            "temp_core_warn": core_warn,
            "temp_mem_warn": mem_warn,
            "temp_mem_crit": mem_crit,
            "power_limit": xml_num(power, "current_power_limit"),
            "power_default": xml_num(power, "default_power_limit"),
            "power_max": xml_num(power, "max_power_limit"),
            "power_min": xml_num(power, "min_power_limit"),
            "vbios": gpu.findtext("vbios_version"),
            "product": gpu.findtext("product_name"),
        }
    return limits


GPU_QUERY = ["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}", "--format=csv,noheader,nounits"]


def parse_gpu_line(line):
    """One card's row of GPU_QUERY output, or None if it is not one."""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != len(GPU_FIELDS):
        return None
    g = {}
    for name, raw in zip(GPU_FIELDS, parts):
        g[name] = num(raw) if name in NUMERIC_GPU_FIELDS else (None if raw in NA_VALUES else raw)
    g["throttle"] = [label for key, label in THROTTLE_LABELS if g.get(key) == "Active"]
    return g


class GpuStream:
    """nvidia-smi in loop mode: one long-lived process, a row per card per second.

    Launching nvidia-smi costs ~0.05 CPU-s, nearly all of it start-up - once a
    second, that was most of what the agent spent after the Proxmox CLIs were
    gone. In loop mode the same readings cost ~0.004 core.

    Each card's latest row is kept with its arrival time, and only fresh rows
    are served: a card that drops off the bus stops being printed, ages out
    and disappears from the count within MAX_AGE - the GPU-count alert still
    works. If the stream stops delivering at all, collect_gpus() falls back to
    one-shot queries, and a stream silent for STALL seconds is killed and
    restarted, so a wedged process costs freshness, never data.
    """

    MAX_AGE = 5.0
    STALL = 10.0

    def __init__(self):
        self.lock = threading.Lock()
        self.latest = {}  # index -> (arrival, row)
        self.proc = None
        self.last_line = 0.0

    def run(self):
        while True:
            try:
                self.proc = subprocess.Popen(
                    [*GPU_QUERY, "-lms", "1000"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                for line in self.proc.stdout:
                    row = parse_gpu_line(line)
                    if row is not None and row.get("index") is not None:
                        now = time.time()
                        with self.lock:
                            self.latest[row["index"]] = (now, row)
                            self.last_line = now
                self.proc.wait()
            except Exception:
                pass
            time.sleep(5)

    def rows(self):
        """Fresh rows by index, or None when the stream is not delivering."""
        now = time.time()
        with self.lock:
            fresh = [r for t, r in self.latest.values() if now - t <= self.MAX_AGE]
            stalled = self.proc is not None and now - self.last_line > self.STALL
        if stalled and self.proc.poll() is None:
            try:
                self.proc.kill()  # run() reads EOF and starts another
            except Exception:
                pass
        return sorted(fresh, key=lambda r: r["index"]) if fresh else None


GPU_STREAM = GpuStream()


def collect_gpus():
    gpus = GPU_STREAM.rows()
    if gpus is None:
        out = run(GPU_QUERY, timeout=10)
        if out is None:
            return {"available": False, "gpus": [], "error": "nvidia-smi failed"}
        gpus = [g for g in map(parse_gpu_line, out.strip().splitlines()) if g is not None]
    procs, _ = CACHES["gpu_procs"].get()
    return {"available": True, "gpus": gpus, "procs": procs or []}


def collect_gpu_procs():
    """What holds GPU memory. Changes on a model switch, not every second."""
    procs = []
    out = run(
        [
            "nvidia-smi",
            "--query-compute-apps=gpu_uuid,pid,used_memory",
            "--format=csv,noheader,nounits",
        ],
        timeout=10,
    )
    for line in (out or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3:
            procs.append({"gpu_uuid": parts[0], "pid": num(parts[1]), "used_mib": num(parts[2])})
    return procs


def collect_xid():
    """NVIDIA Xid faults in the kernel ring buffer - the canary for GPU death."""
    out = run(["dmesg", "-T", "--level=emerg,alert,crit,err,warn"], timeout=15)
    if out is None:
        out = run(["dmesg", "-T"], timeout=15) or ""
    lines = [line for line in out.splitlines() if "xid" in line.lower()]
    return {"count": len(lines), "recent": lines[-8:]}


def collect_pcie():
    """Per-card PCIe throughput - the real inter-GPU activation traffic.

    `nvidia-smi dmon -s t` is the only place this is exposed; the
    `--query-gpu=pcie.*_throughput` fields are not valid on this driver.
    dmon samples at 1 Hz, so this is cached rather than polled hot.
    """
    out = run(["nvidia-smi", "dmon", "-s", "t", "-c", "1"], timeout=12)
    if out is None:
        return {"available": False, "gpus": {}}
    gpus = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()
        if len(f) >= 3:
            idx, rx, tx = num(f[0]), num(f[1]), num(f[2])
            if idx is not None:
                gpus[str(int(idx))] = {"rx_mbs": rx, "tx_mbs": tx}
    return {"available": bool(gpus), "gpus": gpus}
