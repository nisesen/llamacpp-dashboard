"""Proxmox LXC: finding the container that serves llama.cpp, its units, and a
way to run a command inside it. Runs on the Proxmox host.
"""

import json
import os
import shutil
import socket
import time

from .. import config
from ..shell import run, run_bytes
from .streams import ProcessStream
from .units import JOURNAL_TAIL_ARGS, list_units, probe

NAME = "proxmox-lxc"

# How to run a command inside a container. `pct exec` is Proxmox's wrapper
# for exactly this, but it is a Perl CLI that costs ~1 CPU-second per call
# just to start - and with `pct list`/`qm list` beside it, several calls every
# 10 s kept the agent at half a core. `lxc-attach` (with --keep-env) is what
# `pct exec` runs underneath, and costs nothing measurable.
LXC_ATTACH = shutil.which("lxc-attach")


def available():
    return os.path.exists(VMLIST) or shutil.which("pct") is not None


def ct_exec(ctid, *argv):
    """argv that runs `argv` inside container `ctid`."""
    if LXC_ATTACH:
        return [LXC_ATTACH, "-n", str(ctid), "--keep-env", "--", *argv]
    return ["pct", "exec", str(ctid), "--", *argv]


# Proxmox's own record of every guest (id -> node and type), kept current by
# the cluster filesystem. Reading it replaces `pct list` + `qm list`, which
# cost ~1 CPU-second each - every 10 s.
VMLIST = "/etc/pve/.vmlist"


def conf_value(path, key):
    """A top-level key from a Proxmox guest config, e.g. `hostname`.

    Snapshot sections (`[name]`) repeat the keys with the snapshot's old
    values, so only the lines before the first section count.
    """
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("["):
                    break
                k, sep, v = line.partition(":")
                if sep and k.strip() == key:
                    return v.strip()
    except OSError:
        pass
    return ""


def vm_running(vmid):
    """A VM is running when the pid Proxmox recorded is a live QEMU for it."""
    try:
        with open(f"/var/run/qemu-server/{vmid}.pid") as f:
            pid = int(f.read().split()[0])
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f"-id\0{vmid}\0".encode() in f.read()
    except (OSError, ValueError, IndexError):
        return False


def list_guests():
    try:
        with open(VMLIST) as f:
            ids = json.load(f).get("ids") or {}
        running_cts = run(["lxc-ls", "--running"], timeout=10)
    except (OSError, ValueError):
        ids, running_cts = None, None
    if ids is None or running_cts is None:
        return list_guests_cli()
    running_cts = set(running_cts.split())
    node = socket.gethostname().split(".")[0]
    guests = []
    for vmid, meta in ids.items():
        if meta.get("node") not in (None, node):
            continue  # another node of the cluster
        if meta.get("type") == "lxc":
            guests.append(
                {
                    "vmid": vmid,
                    "type": "ct",
                    "status": "running" if vmid in running_cts else "stopped",
                    "name": conf_value(f"/etc/pve/lxc/{vmid}.conf", "hostname"),
                }
            )
        elif meta.get("type") == "qemu":
            guests.append(
                {
                    "vmid": vmid,
                    "type": "vm",
                    "status": "running" if vm_running(vmid) else "stopped",
                    "name": conf_value(f"/etc/pve/qemu-server/{vmid}.conf", "name"),
                }
            )
    guests.sort(key=lambda g: int(g["vmid"]) if g["vmid"].isdigit() else 0)
    return guests


def list_guests_cli():
    """The slow way, for hosts without the files list_guests() reads."""
    guests = []
    for line in (run(["pct", "list"], timeout=20) or "").strip().splitlines()[1:]:
        f = line.split(None, 3)
        if len(f) >= 2:
            guests.append(
                {"vmid": f[0], "status": f[1], "type": "ct", "name": f[-1] if len(f) > 2 else ""}
            )
    # VMs too - a client of the server often runs in one, and `pct list` alone
    # never shows it. Only containers are searched for the inference units.
    for line in (run(["qm", "list"], timeout=20) or "").strip().splitlines()[1:]:
        f = line.split()
        if len(f) >= 3:
            guests.append({"vmid": f[0], "name": f[1], "status": f[2], "type": "vm"})
    guests.sort(key=lambda g: int(g["vmid"]) if g["vmid"].isdigit() else 0)
    return guests


# Unit FILES change when someone adds a model, not every 10 s, so which
# container serves llama.cpp and what its units are called is re-checked on
# a slower clock - and at once if the container found last time stops. The
# units' live state is still read on every refresh. A switch between known
# units needs no re-discovery: list-unit-files includes inactive ones.
DISCOVERY_TTL = 300.0
DISCOVERY_RETRY = 30.0  # while nothing has been found
_discovery = {"at": 0.0, "ctid": None, "units": []}


def find_inference_ct(guests):
    """Which running container serves llama.cpp? Ask, don't assume."""
    running = {
        g["vmid"] for g in guests if g["status"] == "running" and g.get("type", "ct") == "ct"
    }
    d, now = _discovery, time.time()
    ttl = DISCOVERY_TTL if d["ctid"] else DISCOVERY_RETRY
    if now - d["at"] < ttl and (d["ctid"] is None or d["ctid"] in running):
        return d["ctid"], d["units"]
    ctid, names = None, []
    if config.LLM_CTID:
        ctid, names = config.LLM_CTID, units_in(config.LLM_CTID)
    else:
        for g in guests:
            if g["vmid"] in running:
                found = units_in(g["vmid"])
                if found:
                    ctid, names = g["vmid"], found
                    break
    d.update(at=now, ctid=ctid, units=names)
    return ctid, names


def units_in(ctid):
    """Names of units matching the glob inside a container."""
    return list_units(ct_exec(ctid))


def collect():
    guests = list_guests()
    ctid, unit_names = find_inference_ct(guests)
    ct = {
        "vmid": ctid,
        "reachable": False,
        "units": {},
        "discovered": not config.LLM_CTID,
        "runtime": NAME,
        "label": f"CT {ctid}" if ctid else None,
    }
    if not ctid:
        return {"guests": guests, "llm_ct": ct}
    probe(ct_exec(ctid), unit_names, ct)

    # The endpoint the dashboard should talk to, derived from whichever unit
    # is actually running right now.
    for name, u in ct["units"].items():
        if u.get("active") == "active" and u.get("port") and ct.get("ip"):
            ct["endpoint"] = f"http://{ct['ip']}:{int(u['port'])}"
            ct["active_unit"] = name
            break
    return {"guests": guests, "llm_ct": ct}


def follow(ctid, unit):
    return ProcessStream(ct_exec(ctid, "journalctl", "-u", unit, *JOURNAL_TAIL_ARGS))


def read_head(ctid, unit, path, size):
    return run_bytes(ct_exec(ctid, "head", "-c", str(size), path))
