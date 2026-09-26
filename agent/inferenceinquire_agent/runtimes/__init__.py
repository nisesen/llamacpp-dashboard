"""Where llama.cpp runs, and how to reach into it.

A runtime is a module with four functions:

  available()                      can it work on this machine at all
  collect()                        what runs here, and which target serves
                                   llama.cpp: {"guests": [...], "llm_ct": {...}}
  follow(target, unit)             a stream of that unit's log (streams.py), in
                                   journalctl's short-unix shape
  read_head(target, unit, path, n) the first n bytes of a file there: the
                                   model's GGUF header

`llm_ct` (the name is older than the other runtimes) describes the target:
its id (`vmid`), `runtime`, a `label` for people, the llama* units with their
settings, the `active_unit` and the `endpoint` it answers on.

  proxmox-lxc  a container on this Proxmox host (lxc-attach, /etc/pve)
  docker       containers on this host, through the Docker Engine API
  systemd      units on this host
  none         no inference server here: host and GPU telemetry only

AGENT_RUNTIME=auto (the default) uses proxmox-lxc on a Proxmox host, and
otherwise whichever of docker and systemd has a llama* unit, preferring the
one that is serving.
"""

from .. import config
from . import docker, none, proxmox_lxc, systemd

RUNTIMES = {m.NAME: m for m in (proxmox_lxc, docker, systemd, none)}


def candidates(choice):
    if choice != "auto":
        if choice not in RUNTIMES:
            raise SystemExit(f"AGENT_RUNTIME={choice!r}: expected auto or one of {list(RUNTIMES)}")
        return [RUNTIMES[choice]]
    if proxmox_lxc.available():
        return [proxmox_lxc]
    return [m for m in (docker, systemd) if m.available()] or [none]


CANDIDATES = candidates(config.RUNTIME)
# Which runtime answered for a target, so its log and files come from there.
_owner = {}


def collect_guests():
    results = [(rt, rt.collect()) for rt in CANDIDATES]
    ranked = sorted(
        results,
        key=lambda r: (not r[1]["llm_ct"].get("active_unit"), not r[1]["llm_ct"].get("units")),
    )
    rt, out = ranked[0]
    if out["llm_ct"].get("vmid"):
        _owner[out["llm_ct"]["vmid"]] = rt
    return out


def _runtime(target):
    return _owner.get(target, CANDIDATES[0])


def follow(target, unit):
    return _runtime(target).follow(target, unit)


def read_head(target, unit, path, size):
    return _runtime(target).read_head(target, unit, path, size)


__all__ = ["RUNTIMES", "collect_guests", "follow", "read_head"]
