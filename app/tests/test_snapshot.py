"""Slim ticks: only the sections that changed travel."""

from inferenceinquire.snapshot import section_sigs, slim_snapshot

# --------------------------------------------------- slim ticks -----------


def _snap(**over):
    snap = {
        "ts": 1.0,
        "gpus": [{"idx": 0, "pwr": 90}],
        "journal": {
            "slots": [],
            "requests": [{"task": 1}],
            "events": [{"k": 1}],
            "last_line_age": 3.0,
        },
        "ipmi_sensors": [{"name": "CPU Temp", "value": 40}],
        "sel": {"entries": []},
    }
    snap.update(over)
    return snap


def test_first_tick_carries_everything():
    snap = _snap()
    out, kept = slim_snapshot(snap, section_sigs(snap), {})
    assert out is snap and kept == []


def test_unchanged_sections_are_left_out_and_named():
    snap = _snap()
    sigs = section_sigs(snap)
    out, kept = slim_snapshot(snap, sigs, sigs)
    assert set(kept) == {"journal.requests", "journal.events", "ipmi_sensors", "sel"}
    assert "ipmi_sensors" not in out and "sel" not in out
    # The small, always-moving part of the journal still travels.
    assert out["journal"] == {"slots": [], "last_line_age": 3.0}
    assert out["gpus"] == snap["gpus"] and out["ts"] == 1.0


def test_a_changed_section_travels_again():
    old = _snap()
    new = _snap(ipmi_sensors=[{"name": "CPU Temp", "value": 41}])
    new["journal"]["requests"] = [{"task": 1}, {"task": 2}]
    out, kept = slim_snapshot(new, section_sigs(new), section_sigs(old))
    assert out["ipmi_sensors"][0]["value"] == 41
    assert out["journal"]["requests"] == [{"task": 1}, {"task": 2}]
    assert set(kept) == {"journal.events", "sel"}


def test_slimming_never_touches_the_shared_snapshot():
    snap = _snap()
    sigs = section_sigs(snap)
    slim_snapshot(snap, sigs, sigs)
    assert "requests" in snap["journal"] and "ipmi_sensors" in snap
