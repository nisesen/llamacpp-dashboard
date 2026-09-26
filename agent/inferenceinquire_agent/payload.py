"""The cached collectors, their refresh threads, and the JSON document they make."""

import time

from .cache import CACHES, Cache
from .collectors.gguf import active_model_path, collect_model_arch
from .collectors.host import (
    collect_cpu,
    collect_disks,
    collect_diskstats,
    collect_mem,
    collect_net,
    collect_static,
    collect_uptime,
)
from .collectors.ipmi import collect_ipmi, collect_sel
from .collectors.nvidia import (
    collect_gpu_limits,
    collect_gpu_procs,
    collect_gpus,
    collect_pcie,
    collect_xid,
)
from .journal.follower import JOURNAL
from .runtimes import collect_guests

CACHES.update(
    {
        "gpu": Cache(collect_gpus, 1.0, {"available": False, "gpus": []}),
        "gpu_procs": Cache(collect_gpu_procs, 15.0, []),
        "gpu_limits": Cache(collect_gpu_limits, 300.0, {}),
        "ipmi": Cache(collect_ipmi, 20.0, {"available": False, "sensors": []}),
        "sel": Cache(collect_sel, 120.0, {"available": False, "entries": []}),
        "xid": Cache(collect_xid, 30.0, {"count": 0, "recent": []}),
        "disks": Cache(collect_disks, 60.0, []),
        "guests": Cache(collect_guests, 10.0, {"guests": [], "llm_ct": {}}),
        "static": Cache(collect_static, 3600.0, {}),
        "pcie": Cache(collect_pcie, 5.0, {"available": False, "gpus": {}}),
        # Keyed on the loaded model's path: a model switch re-reads the header
        # at once. On the TTL alone the anatomy panel described the previous
        # model for up to 15 minutes after a switch.
        "arch": Cache(
            collect_model_arch, 900.0, {"available": False}, key=lambda: active_model_path()
        ),
    }
)


# Refreshed on their own thread. With one thread for everything, the 3 s guest
# probe and the 1-4 s IPMI read sat in front of the 1 s GPU refresh, and GPU
# readings could be several seconds old - so a 2 s dashboard poll could see
# the same power sample twice and count it twice toward the sustained-draw rule.
FAST_CACHES = ("gpu", "pcie")


def refresher(names, tick):
    while True:
        for name in names:
            if CACHES[name].stale():
                CACHES[name].refresh()
        time.sleep(tick)


def build_payload():
    now = time.time()
    payload = {
        "ts": now,
        "agent_ok": True,
        "cpu": collect_cpu(),
        "mem": collect_mem(),
        "net": collect_net(),
        "disk_io": collect_diskstats(),
        "journal": JOURNAL.snapshot(),
        "uptime": collect_uptime(),
        "ages": {},
    }
    for name, cache in CACHES.items():
        value, ts = cache.get()
        payload[name] = value
        payload["ages"][name] = round(now - ts, 1) if ts else None
    return payload
