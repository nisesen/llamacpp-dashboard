"""systemd on this host: llama-server run as a unit, next to the agent.

For an agent installed on the host itself (deploy/host-agent/). A containerised
agent cannot ask the host's systemd, so it does not use this adapter.
"""

import os
import shutil
import time

from .llama_args import loopback_url
from .streams import ProcessStream
from .units import JOURNAL_TAIL_ARGS, list_units, probe, serving

NAME = "systemd"
TARGET = "host"

# Which units match changes when someone adds a model, not every 10 s.
DISCOVERY_TTL = 300.0
DISCOVERY_RETRY = 30.0
_discovery = {"at": 0.0, "units": []}


def available():
    """A host booted with systemd, whose systemctl the agent can run."""
    return os.path.isdir("/run/systemd/system") and shutil.which("systemctl") is not None


def unit_names():
    d, now = _discovery, time.time()
    if now - d["at"] >= (DISCOVERY_TTL if d["units"] else DISCOVERY_RETRY):
        d.update(at=now, units=list_units([]))
    return d["units"]


def collect():
    names = unit_names()
    target = {
        "vmid": TARGET if names else None,
        "reachable": False,
        "units": {},
        "discovered": True,
        "runtime": NAME,
        "label": "this host" if names else None,
    }
    if not names:
        return {"guests": [], "llm_ct": target}
    probe([], names, target, guest=False)
    name, port = serving(target["units"])
    if name:
        target["endpoint"] = loopback_url(target["units"][name].get("bind"), port)
        target["active_unit"] = name
    return {"guests": [], "llm_ct": target}


def follow(target, unit):
    return ProcessStream(["journalctl", "-u", unit, *JOURNAL_TAIL_ARGS])


def read_head(target, unit, path, size):
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return None
