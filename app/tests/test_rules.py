"""The alert rules."""

from inferenceinquire import config
from inferenceinquire.rules import evaluate_alerts
from support import evaluate, healthy_snap, make_store


def test_healthy_machine_raises_nothing(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    assert evaluate(st, healthy_snap(), {}) == set()


SLOW = {"decode_tps": 2.5, "gen_tokens": 300, "decode_baseline": 44.0}
FAST = {"decode_tps": 44.0, "gen_tokens": 300, "decode_baseline": 44.0}
NO_OBSERVATION = {"decode_tps": None, "gen_tokens": 0, "decode_baseline": 44.0}


def test_cpu_fallback_detected_against_learned_baseline(tmp_path):
    # A real fallback makes every observation slow, so it reaches the
    # threshold within a few requests.
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    for _ in range(config.SLOW_RUN_TO_ALERT):
        got = evaluate(st, healthy_snap(), dict(SLOW))
    assert "slow_decode" in got


def test_one_slow_observation_is_not_an_alert(tmp_path):
    # The false positive this rule used to produce: a single request that
    # shared the GPU with another slot's prefill reports a per-slot rate far
    # below the single-stream baseline while the box is entirely healthy.
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    assert "slow_decode" not in evaluate(st, healthy_snap(), dict(SLOW))


def test_a_normal_observation_resets_the_run(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    for _ in range(config.SLOW_RUN_TO_ALERT - 1):
        evaluate(st, healthy_snap(), dict(SLOW))
    evaluate(st, healthy_snap(), dict(FAST))
    assert "slow_decode" not in evaluate(st, healthy_snap(), dict(SLOW))


def test_polls_without_an_observation_do_not_break_the_run(tmp_path):
    # The counters only move when a request finishes, so most polls carry no
    # decode rate. If those reset the run, a real fallback never alerts.
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    for _ in range(config.SLOW_RUN_TO_ALERT):
        evaluate(st, healthy_snap(), dict(NO_OBSERVATION))
        got = evaluate(st, healthy_snap(), dict(SLOW))
        evaluate(st, healthy_snap(), dict(NO_OBSERVATION))
    assert "slow_decode" in got


def test_slow_decode_is_held_between_observations(tmp_path):
    # Once believed it stays raised on observation-less polls, so a fallback
    # reads as one sustained critical instead of a blip per request.
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    for _ in range(config.SLOW_RUN_TO_ALERT):
        evaluate(st, healthy_snap(), dict(SLOW))
    assert "slow_decode" in evaluate(st, healthy_snap(), dict(NO_OBSERVATION))


def test_slow_decode_clears_once_speed_returns(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    for _ in range(config.SLOW_RUN_TO_ALERT):
        evaluate(st, healthy_snap(), dict(SLOW))
    assert "slow_decode" not in evaluate(st, healthy_snap(), dict(FAST))


def test_normal_speed_for_a_slow_model_is_not_an_alert(tmp_path):
    # The whole point of learning the baseline: a model that simply runs at
    # 8 tok/s must not trip a fixed "under 10" rule forever.
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    point = {"decode_tps": 8.0, "gen_tokens": 300, "decode_baseline": 9.0}
    assert "slow_decode" not in evaluate(st, healthy_snap(), point)


def test_slow_decode_ignored_when_barely_any_tokens_generated(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    point = {"decode_tps": 1.0, "gen_tokens": 3, "decode_baseline": 44.0}
    assert "slow_decode" not in evaluate(st, healthy_snap(), point)


def test_gpu_thresholds_come_from_the_card(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    snap = healthy_snap()
    snap["gpus"][0]["hbm"] = 95  # over the card's reported warn (90)
    assert "hbm:0" in evaluate(st, snap, {})

    snap2 = healthy_snap()
    snap2["gpus"][0]["limits"]["temp_mem_warn"] = 120  # a hotter-rated card
    snap2["gpus"][0]["hbm"] = 95
    assert "hbm:0" not in evaluate(st, snap2, {})


def over_cap(watts=320):
    snap = healthy_snap()
    snap["gpus"][0]["pwr"] = watts  # 1.28x of a 250 W cap
    return snap


def test_power_alert_is_relative_to_each_card_cap(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    snap = over_cap()
    for _ in range(config.POWER_RUN_TO_ALERT):
        got = evaluate(st, snap, {})
    assert "pwr:0" in got

    snap["gpus"][0]["pwr_limit"] = 375  # uncapped card, same draw -> fine
    assert "pwr:0" not in evaluate(st, snap, {})


def test_one_reading_over_the_cap_is_a_prefill_spike_not_an_alert(tmp_path):
    # 75% of this box's real episodes are exactly one 2 s poll long, and the
    # longest ever recorded was four. Prefill legitimately overshoots the cap.
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    assert "pwr:0" not in evaluate(st, over_cap(), {})


def test_a_normal_power_reading_resets_the_run(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    for _ in range(config.POWER_RUN_TO_ALERT - 1):
        evaluate(st, over_cap(), {})
    evaluate(st, healthy_snap(), {})  # 90 W, well under
    assert "pwr:0" not in evaluate(st, over_cap(), {})


def test_sustained_draw_takes_its_level_from_the_peak(tmp_path):
    # Spikes to 1.4x then settles at 1.28x: still serious, because it got there.
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    evaluate(st, over_cap(350), {})  # 1.40x of 250 W
    for _ in range(config.POWER_RUN_TO_ALERT - 1):
        alerts = evaluate_alerts(over_cap(320), {}, st)
    assert [a["level"] for a in alerts if a["key"] == "pwr:0"] == ["serious"]


def test_missing_gpu_is_critical(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 2)  # two were seen before
    assert "gpu_count" in evaluate(st, healthy_snap(), {})


def test_remapped_rows_alert_only_on_growth(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    snap = healthy_snap()
    snap["gpus"][0]["remap_uncorrectable"] = 16
    # First sight establishes the baseline - a card with history is not a fault.
    assert "remap:SN1" not in evaluate(st, snap, {})
    # Growth past it is.
    snap["gpus"][0]["remap_uncorrectable"] = 17
    assert "remap:SN1" in evaluate(st, snap, {})


def test_two_models_running_at_once_is_critical(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    snap = healthy_snap()
    snap["units"]["llama-flashnext"] = {"active": "active", "restarts": 0}
    assert "units_both" in evaluate(st, snap, {})


def test_no_unit_running_is_critical(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    snap = healthy_snap()
    snap["units"]["llama-server"]["active"] = "inactive"
    assert "units" in evaluate(st, snap, {})


def test_ipmi_uses_each_sensors_own_thresholds(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    snap = healthy_snap()
    snap["ipmi_sensors"] = [
        {
            "name": "12V",
            "kind": "volt",
            "value": 10.9,
            "unit": "Volts",
            "state": "ok",
            "thresholds": {"lcr": 11.0, "ucr": 13.7},
        }
    ]
    assert "ipmi_lo:12V" in evaluate(st, snap, {})


# ------------------------------------------------------------ BMC events --


def sel_snap(entries, max_rid, ts=1_000_000.0):
    snap = healthy_snap()
    snap["ts"] = ts
    snap["sel"] = {"available": True, "max_real_rid": max_rid, "entries": entries}
    return snap


def test_bmc_log_alerts_only_on_new_records(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    old = [{"rid": 46, "what": "Memory | Uncorrectable ECC (@DIMMG1(CPU1)) | Asserted"}]
    # History on first sight is the baseline, not news.
    assert "sel" not in evaluate(st, sel_snap(old, 46), {})
    new = [*old, {"rid": 47, "what": "Power Supply | Failure detected | Asserted"}]
    alerts = evaluate_alerts(sel_snap(new, 47), {}, st)
    got = [a for a in alerts if a["key"] == "sel"]
    assert got and got[0]["level"] == "serious" and "Power Supply" in got[0]["title"]
    # Still shown next poll, gone a day later.
    assert "sel" in evaluate(st, sel_snap(new, 47, 1_000_060.0), {})
    assert "sel" not in evaluate(st, sel_snap(new, 47, 1_000_000.0 + 86_401), {})


def test_bmc_benign_record_is_a_warning(tmp_path):
    st = make_store(tmp_path)
    st.set_kv("gpu_count_baseline", 1)
    evaluate(st, sel_snap([], 10), {})
    alerts = evaluate_alerts(
        sel_snap([{"rid": 11, "what": "Chassis Intru | Asserted"}], 11), {}, st
    )
    assert [a["level"] for a in alerts if a["key"] == "sel"] == ["warning"]


# --------------------------------------- request telemetry ---------------


def telemetry(st, tok, ev, journal=None):
    snap = {
        "journal": journal or {"following": "llama-server", "alive": True},
        "model": {"build": "b10935-8e330954a"},
    }
    return [
        a
        for a in evaluate_alerts(snap, {"tok_total": tok, "parsed_timings": ev}, st)
        if a["key"] == "journal_parse"
    ]


def test_requests_finishing_without_timings_raise_the_notice(tmp_path):
    st = make_store(tmp_path)
    assert telemetry(st, 1000, 50) == []
    # /metrics says requests finished; the log yields no timings for them.
    assert telemetry(st, 1300, 50) == []
    assert telemetry(st, 1600, 50) == []
    got = telemetry(st, 1900, 50)
    assert got and got[0]["level"] == "warning" and "log format" in got[0]["title"]
    assert "b10935" in got[0]["detail"]
    # Polls where nothing finished neither add to the count nor clear it.
    assert telemetry(st, 1900, 50)


def test_parsed_timings_clear_the_notice(tmp_path):
    st = make_store(tmp_path)
    for tok in (1000, 1300, 1600, 1900):
        telemetry(st, tok, 50)
    assert telemetry(st, 2200, 51) == []
    assert telemetry(st, 2500, 51) == []


def test_timings_arriving_a_poll_late_are_not_a_miss(tmp_path):
    """The log line can land a poll after /metrics moves."""
    st = make_store(tmp_path)
    ev = 50
    for tok in range(1000, 4000, 300):
        assert telemetry(st, tok, ev) == []
        ev += 1


def test_an_agent_without_counts_is_left_alone(tmp_path):
    st = make_store(tmp_path)
    for tok in (1000, 1300, 1600, 1900, 2200):
        assert telemetry(st, tok, None) == []
    assert st.get_kv(config.PARSE_WATCH_KEY) is None


def test_a_server_restart_is_not_a_miss(tmp_path):
    st = make_store(tmp_path)
    telemetry(st, 5000, 80)
    for tok in (10, 20, 30):  # counters restarted lower, then grow
        telemetry(st, tok, 80)
    assert len(telemetry(st, 40, 80)) == 1  # still three finishes without timings
    st2 = make_store(tmp_path / "b")
    telemetry(st2, 5000, 80)
    assert telemetry(st2, 10, 80) == []
