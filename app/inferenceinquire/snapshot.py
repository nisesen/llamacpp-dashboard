"""The snapshot as the page receives it: trimmed live points, and only the
sections that changed since the client's last tick.
"""

from __future__ import annotations

import hashlib
import json

POINT_KEYS = [
    "ts",
    "decode_tps",
    "prefill_tps",
    "req_proc",
    "req_def",
    "accept_rate",
    "accepted_per_draft",
    "cache_hit",
    "ctx_used",
    "busy_slots",
    "cpu_pct",
    "ram_pct",
    "load1",
    "net_rx",
    "net_tx",
    "cpu_temp",
    "sys_temp",
    "v12",
    "healthy",
    "decode_baseline",
    "tg_live",
    "disk_read",
]


def strip_point(point: dict) -> dict:
    out = {k: point.get(k) for k in POINT_KEYS}
    out["gpus"] = point.get("gpus", [])
    return out


# Parts of the snapshot that change far less often than every poll. A tick
# carries one only when it differs from what that client already holds, and
# names the ones it left out under "kept"; the page keeps its own copy. The
# journal's request and event lists alone were 19 KB of a 37 KB tick, resent
# every 2 s whether or not a single line had arrived.
SLIM_PATHS = (
    ("journal", "requests"),
    ("journal", "events"),
    ("ipmi_sensors",),
    ("sel",),
    ("arch",),
    ("static",),
    ("llm_ct",),
    ("units",),
    ("guests",),
    ("disks",),
    ("stats",),
    ("xid",),
    ("gpu_procs",),
    ("spec_positions",),
    ("probe_signature",),
    ("last",),
    ("slots",),
    ("totals",),
    ("model",),
)
_MISSING = object()


def section_sigs(snap: dict) -> dict[str, bytes]:
    """A short digest of each slimmable section, keyed 'a' or 'a.b'."""
    sigs = {}
    for path in SLIM_PATHS:
        v = snap
        for k in path:
            v = v.get(k, _MISSING) if isinstance(v, dict) else _MISSING
        if v is not _MISSING:
            sigs[".".join(path)] = hashlib.blake2b(
                json.dumps(v, sort_keys=True, default=str).encode(), digest_size=8
            ).digest()
    return sigs


def slim_snapshot(snap: dict, sigs: dict, known: dict) -> tuple[dict, list[str]]:
    """The snapshot minus the sections this client already holds unchanged.

    Never mutates `snap`: it is shared with every client and with /api/state.
    """
    kept = [p for p, sig in sigs.items() if known.get(p) == sig]
    if not kept:
        return snap, []
    out = dict(snap)
    for p in kept:
        top, _, sub = p.partition(".")
        if sub:
            out[top] = {k: v for k, v in out[top].items() if k != sub}
        else:
            del out[top]
    return out, kept
