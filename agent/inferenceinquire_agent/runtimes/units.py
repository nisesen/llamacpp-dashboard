"""systemd units that serve llama.cpp, wherever that systemd runs.

Shared by the Proxmox adapter, which reaches into a container, and the systemd
adapter, which runs on this host. Both pass `prefix`: the argv that runs a
command where the units live (empty for this host).
"""

from .. import config
from ..shell import num, run
from .llama_args import settings_from_args

# How a unit's log is followed. Only the last 40 lines on a respawn: the parser
# counts against a high-water mark, so a re-read burst is not counted twice.
JOURNAL_TAIL_ARGS = ["-f", "-n", "40", "-o", "short-unix", "--no-pager"]


def list_units(prefix):
    """Names of units matching the glob."""
    out = run(
        [*prefix, "systemctl", "list-unit-files", config.UNIT_GLOB, "--no-legend", "--no-pager"],
        timeout=25,
    )
    names = []
    for line in (out or "").splitlines():
        parts = line.split()
        if parts and parts[0].endswith(".service"):
            names.append(parts[0][: -len(".service")])
    return names


# What the guest itself looks like: only asked of a container, not of a host
# the agent already reports on.
GUEST_LINES = r"""
    printf 'UPTIME:'; cat /proc/uptime
    printf 'LOAD:';   cat /proc/loadavg
    printf 'MEM:';    awk '/MemTotal|MemAvailable/{printf "%s ", $2}' /proc/meminfo; echo
    printf 'DF:';     df -B1 --output=size,used / | tail -1
    printf 'IP:';     hostname -I
"""
UNIT_LINES = r"""
    for u in __UNITS__; do
      printf 'UNIT:%s:' "$u"
      systemctl show "$u" -p ActiveState -p SubState -p NRestarts \
        -p ExecMainStartTimestampMonotonic -p MemoryCurrent 2>/dev/null \
        | tr '\n' '|'
      echo
      printf 'EXEC:%s:' "$u"
      systemctl show "$u" -p ExecStart --value 2>/dev/null | tr '\n' ' '
      echo
      # When the unit last became active, as epoch seconds. Guarded:
      # `date -d ""` means midnight today, not "never".
      since=$(systemctl show "$u" -p ActiveEnterTimestamp --value 2>/dev/null)
      printf 'SINCE:%s:' "$u"
      [ -n "$since" ] && date -d "$since" +%s 2>/dev/null
      echo
    done
"""


def probe(prefix, unit_names, target, guest=True):
    """One round trip for the units' state and settings (and, for a guest,
    its uptime, load, memory, disk and address), parsed into `target`."""
    script = (GUEST_LINES if guest else "") + UNIT_LINES.replace(
        "__UNITS__", " ".join(unit_names) if unit_names else ""
    )
    out = run([*prefix, "bash", "-c", script], timeout=30)
    target["reachable"] = out is not None
    for line in (out or "").splitlines():
        if line.startswith("UPTIME:"):
            target["uptime"] = float(line[7:].split()[0])
        elif line.startswith("LOAD:"):
            p = line[5:].split()
            target["load"] = [float(p[0]), float(p[1]), float(p[2])]
        elif line.startswith("MEM:"):
            p = line[4:].split()
            if len(p) >= 2:
                target["mem_total"] = int(p[0]) * 1024
                target["mem_used"] = (int(p[0]) - int(p[1])) * 1024
        elif line.startswith("DF:"):
            p = line[3:].split()
            if len(p) >= 2:
                target["disk_total"], target["disk_used"] = int(p[0]), int(p[1])
        elif line.startswith("IP:"):
            ips = line[3:].split()
            if ips:
                target["ip"] = ips[0]
        elif line.startswith("UNIT:"):
            _, unit, rest = line.split(":", 2)
            props = {}
            for kv in rest.split("|"):
                k, _, v = kv.partition("=")
                if k:
                    props[k.strip()] = v
            target["units"].setdefault(unit, {}).update(
                {
                    "active": props.get("ActiveState"),
                    "sub": props.get("SubState"),
                    "restarts": num(props.get("NRestarts", "0")),
                    "memory": num(props.get("MemoryCurrent", "")),
                }
            )
        elif line.startswith("SINCE:"):
            _, unit, rest = line.split(":", 2)
            # The container's uptime is not the service's: a restarted unit in
            # a long-running container read "up 5d" minutes after a restart.
            target["units"].setdefault(unit, {})["active_since"] = num(rest.strip())
        elif line.startswith("EXEC:"):
            _, unit, rest = line.split(":", 2)
            target["units"].setdefault(unit, {}).update(settings_from_args(rest))
    return target


def serving(units):
    """(name, port) of the unit running right now, or (None, None)."""
    for name, u in units.items():
        if u.get("active") == "active" and u.get("port"):
            return name, int(u["port"])
    return None, None
