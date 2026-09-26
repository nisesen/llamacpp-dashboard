"""The host itself, from /proc and /sys: CPU, memory, network, disks, uptime."""

import os
import socket

from .. import config
from ..shell import run


def collect_cpu():
    """Raw jiffy counters - the dashboard differences them into percentages."""
    total, per_core = None, []
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            fields = line.split()
            vals = [int(v) for v in fields[1:11]]
            busy = sum(vals) - vals[3] - vals[4]  # minus idle and iowait
            entry = [sum(vals), busy]
            if fields[0] == "cpu":
                total = entry
            else:
                per_core.append(entry)

    mhz = []
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("cpu MHz"):
                    mhz.append(float(line.split(":")[1]))
    except Exception:
        pass

    with open("/proc/loadavg") as f:
        load = f.read().split()

    return {
        "total": total,
        "per_core": per_core,
        "cores": len(per_core),
        "load": [float(load[0]), float(load[1]), float(load[2])],
        "procs_running": load[3],
        "mhz_avg": round(sum(mhz) / len(mhz), 1) if mhz else None,
        "mhz_max": round(max(mhz), 1) if mhz else None,
    }


def collect_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            info[k] = int(v.split()[0]) * 1024  # kB -> bytes
    total = info.get("MemTotal", 0)
    return {
        "total": total,
        "available": info.get("MemAvailable", 0),
        "used": total - info.get("MemAvailable", 0),
        "free": info.get("MemFree", 0),
        "cached": info.get("Cached", 0) + info.get("SReclaimable", 0),
        "buffers": info.get("Buffers", 0),
        "swap_total": info.get("SwapTotal", 0),
        "swap_used": info.get("SwapTotal", 0) - info.get("SwapFree", 0),
        "dirty": info.get("Dirty", 0),
    }


def default_route_iface():
    """The interface carrying the default route - no bridge name assumed."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) > 1 and fields[1] == "00000000":
                    return fields[0]
    except Exception:
        pass
    return None


def collect_net():
    """Raw byte counters for every real interface, plus which one is primary."""
    primary = default_route_iface()
    out = {"primary": primary, "ifaces": {}}
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            # Skip loopback and per-guest veth pairs: the guest's own traffic
            # already shows up on the bridge.
            if name == "lo" or name.startswith(("veth", "fw", "tap")):
                continue
            c = rest.split()
            out["ifaces"][name] = {
                "rx": int(c[0]),
                "tx": int(c[8]),
                "rx_err": int(c[2]),
                "tx_err": int(c[10]),
                "rx_drop": int(c[3]),
                "tx_drop": int(c[11]),
            }
    return out


def collect_uptime():
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def collect_disks():
    disks = []
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        disks.append(
            {"name": "host root", "mount": "/", "total": total, "used": total - free, "kind": "fs"}
        )
    except Exception:
        pass
    for line in (run(["pvesm", "status"], timeout=20) or "").strip().splitlines()[1:]:
        f = line.split()
        if len(f) >= 6 and f[2] == "active":
            disks.append(
                {
                    "name": f[0],
                    "mount": f"pvesm:{f[1]}",
                    "total": int(f[3]) * 1024,
                    "used": int(f[4]) * 1024,
                    "kind": "storage",
                }
            )
    return disks


def collect_static():
    drv = (
        (
            run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], timeout=10)
            or ""
        )
        .strip()
        .splitlines()
    )
    cpu_model = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass
    return {
        "hostname": socket.gethostname(),
        "kernel": os.uname().release,
        "pve_version": (run(["pveversion"], timeout=15) or "").strip(),
        "driver_version": drv[0] if drv else None,
        "cpu_model": cpu_model,
        "agent_version": config.AGENT_VERSION,
    }


def collect_diskstats():
    """Raw sector counters for physical disks; the dashboard differences them.

    Model weights stream from here at load, so this is what makes the load
    sequence visible rather than inferred.
    """
    out = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p) < 14:
                    continue
                name = p[2]
                # Whole devices only - partitions and device-mapper would
                # double count the same bytes.
                if not (name.startswith("nvme") and name.endswith("n1")) and not (
                    len(name) == 3 and name.startswith("sd")
                ):
                    continue
                out[name] = {"read_sectors": int(p[5]), "write_sectors": int(p[9])}
    except Exception:
        pass
    return out
