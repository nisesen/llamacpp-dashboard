"""The BMC: sensors with their own thresholds, and the event log (SEL)."""

import re
import shutil
import time

from ..shell import num, run


def classify_sensor(name, unit):
    if "degrees" in unit:
        return "temp"
    if "Volts" in unit:
        return "volt"
    if "RPM" in unit:
        return "fan"
    if "Watts" in unit:
        return "watt"
    if "Amps" in unit:
        return "amp"
    return "other"


def collect_ipmi():
    if not shutil.which("ipmitool"):
        return {"available": False, "sensors": []}
    out = run(["ipmitool", "sensor"], timeout=25)
    if out is None:
        return {"available": False, "sensors": []}
    sensors = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        name, raw, unit, state = parts[0], parts[1], parts[2], parts[3]
        value = num(raw)
        if value is None:
            continue  # unpopulated sensor
        thresholds = {}
        for key, idx in (("lnr", 4), ("lcr", 5), ("lnc", 6), ("unc", 7), ("ucr", 8), ("unr", 9)):
            if len(parts) > idx:
                t = num(parts[idx])
                if t is not None:
                    thresholds[key] = t
        sensors.append(
            {
                "name": name,
                "value": value,
                "unit": unit.strip(),
                "state": state,
                "kind": classify_sensor(name, unit),
                "thresholds": thresholds,
            }
        )
    return {"available": True, "sensors": sensors}


# Records a BMC writes with no sensor behind them, typically on a reset while
# its clock is unset. They can carry record IDs *above* the real events (and
# a date years in the past), so "the last 12 by ID" can be nothing but these,
# hiding a board's genuine ECC or power history. Counted, not shown.
SEL_NOISE = ("Unknown #0xff",)
SEL_KEEP = 12


def parse_sel(text):
    """`ipmitool sel elist` -> entries, oldest record first, noise flagged."""
    entries = []
    for line in (text or "").strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        try:
            rid = int(parts[0], 16)  # record IDs are hex
        except ValueError:
            continue
        what = " | ".join(p for p in parts[3:] if p)
        ts = None
        try:
            # The zone abbreviation (PST/PDT) is the host's own; mktime
            # applies local DST, which is what wrote it.
            ts = time.mktime(
                time.strptime(f"{parts[1]} {parts[2].rsplit(' ', 1)[0]}", "%m/%d/%Y %I:%M:%S %p")
            )
        except (ValueError, IndexError):
            pass
        entries.append(
            {
                "id": parts[0],
                "rid": rid,
                "ts": ts,
                "when": " ".join(parts[1:3]),
                "what": what,
                "noise": any(n in what for n in SEL_NOISE),
            }
        )
    entries.sort(key=lambda e: e["rid"])
    return entries


def collect_sel():
    if not shutil.which("ipmitool"):
        return {"available": False, "entries": [], "count": 0}
    out = run(["ipmitool", "sel", "elist"], timeout=25)
    if out is None:
        return {"available": False, "entries": [], "count": 0}
    all_entries = parse_sel(out)
    real = [e for e in all_entries if not e["noise"]]
    info = run(["ipmitool", "sel", "info"], timeout=25) or ""
    m = re.search(r"Entries\s*:\s*(\d+)", info)
    return {
        "available": True,
        # Newest real events last, as before; the noise is counted, not shown.
        "entries": real[-SEL_KEEP:],
        "count": int(m.group(1)) if m else len(all_entries),
        "real_count": len(real),
        "noise_count": len(all_entries) - len(real),
        # What the dashboard baselines against: a new real record gets a
        # higher ID than any it has seen.
        "max_real_rid": max((e["rid"] for e in real), default=0),
    }
