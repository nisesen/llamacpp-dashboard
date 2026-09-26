"""Holds, model-switch transitions, push policy and severity order."""

from inferenceinquire import config
from inferenceinquire.alerts import AlertGate, should_notify, transition_signal, worst_level


def test_worst_level_orders_by_severity():
    assert worst_level([]) == "good"
    assert worst_level([{"level": "warning"}, {"level": "critical"}]) == "critical"
    assert worst_level([{"level": "warning"}, {"level": "serious"}]) == "serious"


# ----------------------------------------------- holds and model switches --

UNREACHABLE = {
    "key": "llama",
    "level": "critical",
    "title": "Inference server unreachable",
    "detail": "",
}


def test_reachability_must_hold_before_it_alerts():
    hold = config.HOLD_SECONDS["llama"]
    gate = AlertGate()
    assert gate.apply([UNREACHABLE], 0.0) == []
    # The switch gap measured on this box was 6-8 s: never an alert on its own.
    assert gate.apply([UNREACHABLE], 8.0) == []
    shown = gate.apply([UNREACHABLE], hold + 1.0)
    assert [a["key"] for a in shown] == ["llama"] and shown[0]["since"] == 0.0
    # Clearing resets the clock.
    gate.apply([], hold + 2.0)
    assert gate.apply([UNREACHABLE], hold + 3.0) == []


def test_hardware_alerts_are_not_delayed():
    gate = AlertGate()
    xid = {"key": "xid", "level": "critical", "title": "1 Xid fault", "detail": ""}
    assert [a["key"] for a in gate.apply([xid], 0.0)] == ["xid"]


def test_a_model_switch_is_a_state_not_an_outage():
    gate = AlertGate()
    snap = {
        "units": {
            "llama-server": {"active": "deactivating"},
            "llama-flashnext": {"active": "activating"},
        }
    }
    assert gate.update_transition(transition_signal(snap), 0.0)[0] == "started"
    for t in range(0, 60, 2):  # the load, as the switch script runs it
        gate.update_transition(transition_signal(snap), float(t))
        assert gate.apply([UNREACHABLE], float(t)) == []
    # Loaded: the signal stops; the tail covers the agent's stale unit cache...
    assert gate.update_transition(None, 70.0) is None and gate.suppressing(70.0)
    # ...then the edge is reported, and nothing critical was ever shown.
    assert gate.update_transition(None, 80.0)[0] == "ended"


def test_a_load_that_fails_alerts_when_the_transition_ends():
    gate = AlertGate()
    gate.update_transition("llama-server activating", 0.0)
    for t in range(0, 40, 2):
        gate.apply([UNREACHABLE], float(t))
    gate.update_transition(None, 61.0)  # unit gave up; tail expired
    shown = gate.apply([UNREACHABLE], 61.0)
    assert [a["key"] for a in shown] == ["llama"]


def test_a_load_that_never_finishes_alerts_after_the_limit():
    gate = AlertGate()
    gate.update_transition("llama-server activating", 0.0)
    gate.apply([UNREACHABLE], 0.0)
    gate.update_transition("llama-server activating", config.TRANSITION_MAX + 1)
    assert [a["key"] for a in gate.apply([UNREACHABLE], config.TRANSITION_MAX + 1)] == ["llama"]
    assert gate.view(config.TRANSITION_MAX + 1)["stalled"]


def test_transition_signals():
    assert transition_signal({"llama": {"loading": True}})
    assert (
        transition_signal({"journal": {"load": {"state": "loading", "path": "/m/x.gguf"}}})
        == "loading x.gguf"
    )
    assert transition_signal({"units": {"llama-server": {"active": "active"}}}) is None


# ------------------------------------------------------------------ push --


def test_push_policy(monkeypatch):
    # By default everything serious is push-worthy, reachability included.
    # Pinned: the image's environment carries the site's own PUSH_SKIP_KEYS.
    monkeypatch.setattr(config, "PUSH_SKIP_KEYS", set())
    assert should_notify("llama", "critical")
    assert should_notify("gpu_count", "critical")
    assert should_notify("agent", "serious")
    assert should_notify("pwr:1", "warning")
    assert should_notify("sel", "warning")
    assert not should_notify("disk:local", "warning")
    assert not should_notify("transition", "info")


def test_push_skip_keys_leave_out_what_another_monitor_reports(monkeypatch):
    monkeypatch.setattr(config, "PUSH_SKIP_KEYS", {"llama", "llama_health", "units"})
    assert not should_notify("llama", "critical")
    assert not should_notify("units", "critical")
    assert should_notify("gpu_count", "critical")
