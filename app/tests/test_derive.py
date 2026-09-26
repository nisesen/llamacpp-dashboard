"""Rates, percentages, sensor picking and the arithmetic of each poll."""

from inferenceinquire.derive import (
    counter_deltas,
    counter_rates,
    cpu_usage,
    disk_rates,
    host_power,
    lifetime_totals,
    live_decode,
    net_rates,
    pct,
    pick_sensor,
    rate,
    slot_rows,
)

# ------------------------------------------------------------------ rates --


def test_rate_returns_none_when_no_time_passed():
    # The key property: an idle interval must read as "no measurement",
    # not as zero tok/s, or every idle poll would look like a stall.
    assert rate(0, 0) is None
    assert rate(100, None) is None


def test_rate_divides():
    assert rate(100, 2.0) == 50.0


def test_pct():
    assert pct(50, 200) == 25.0
    assert pct(1, 0) is None
    assert pct(None, 100) == 0.0


# ---------------------------------------------------------------- sensors --

SENSORS = [
    {"name": "CPU Temp", "kind": "temp", "value": 31, "unit": "degrees C", "state": "ok"},
    {"name": "System Temp", "kind": "temp", "value": 39, "unit": "degrees C", "state": "ok"},
    {"name": "12V", "kind": "volt", "value": 11.98, "unit": "Volts", "state": "ok"},
]


def test_pick_sensor_matches_by_pattern_not_exact_name():
    assert pick_sensor(SENSORS, "temp", ("cpu",)) == 31
    assert pick_sensor(SENSORS, "volt", ("12v",)) == 11.98
    # Wrong kind must not match even when the name would.
    assert pick_sensor(SENSORS, "fan", ("cpu",)) is None


def test_host_power_prefers_a_total_then_sums_supplies():
    def w(n, v):
        return {"kind": "watt", "name": n, "value": v}

    assert host_power([w("PSU1 Input Power", 180), w("Pwr Consumption", 400)]) == 400
    assert host_power([w("PSU1 Input Power", 180), w("PSU2 Input Power", 170)]) == 350
    assert host_power([{"kind": "temp", "name": "CPU Temp", "value": 40}]) is None


# ------------------------------------------------ poll arithmetic ---------

COUNTERS = {
    "llamacpp:tokens_predicted_total": 1000.0,
    "llamacpp:tokens_predicted_seconds_total": 20.0,
    "llamacpp:prompt_tokens_total": 5000.0,
    "llamacpp:prompt_seconds_total": 5.0,
    "llamacpp:prompt_tokens_cached_total": 15000.0,
    "llamacpp:spec_decode_num_draft_tokens_total": 300.0,
    "llamacpp:spec_decode_num_accepted_tokens_total": 150.0,
    "llamacpp:spec_decode_num_drafts_total": 100.0,
}


def test_counter_rates_are_differences_between_polls():
    later = dict(COUNTERS)
    later["llamacpp:tokens_predicted_total"] += 100
    later["llamacpp:tokens_predicted_seconds_total"] += 2
    later["llamacpp:prompt_tokens_total"] += 1000
    later["llamacpp:prompt_seconds_total"] += 1
    later["llamacpp:prompt_tokens_cached_total"] += 3000
    p = counter_rates(later, COUNTERS)
    assert p["decode_tps"] == 50.0 and p["prefill_tps"] == 1000.0
    assert p["gen_tokens"] == 100 and p["cache_hit"] == 0.75
    # Nothing drafted since the last poll: no acceptance figure, not zero.
    assert p["accept_rate"] is None


def test_counter_rates_without_a_previous_poll_say_nothing():
    p = counter_rates(COUNTERS, None)
    assert p["decode_tps"] is None and p["gen_tokens"] == 0 and p["cache_hit"] is None
    assert p["tok_total"] == 1000.0


def test_lifetime_totals_use_the_counters():
    t = lifetime_totals(COUNTERS)
    assert t["decode_tps_avg"] == 50.0 and t["prefill_tps_avg"] == 1000.0
    assert t["accept_rate_avg"] == 0.5 and t["accepted_per_draft_avg"] == 1.5
    assert t["cache_hit_avg"] == 0.75
    assert lifetime_totals({})["decode_tps_avg"] is None


def test_slot_rows_take_the_deepest_context_and_a_sleeping_model_has_none():
    raw = [
        {"id": 0, "n_prompt_tokens": 900, "next_token": [{"n_decoded": 12}]},
        {"id": 1, "n_prompt_tokens": 40000, "is_processing": True, "next_token": {}},
        "junk",
    ]
    slots, ctx = slot_rows(raw, sleeping=False)
    assert [s["id"] for s in slots] == [0, 1] and ctx == 40000
    assert slots[0]["decoded"] == 12 and slots[1]["processing"]
    assert slot_rows(None, sleeping=True) == ([], None)
    assert slot_rows(None, sleeping=False) == ([], 0)


def test_cpu_usage_needs_two_readings():
    first = {"total": [1000, 250], "per_core": [[500, 100], [500, 150]]}
    later = {"total": [1400, 450], "per_core": [[700, 200], [700, 250]]}
    assert cpu_usage(first, None) == (None, [])
    assert cpu_usage(later, first) == (50.0, [50.0, 50.0])


def test_rates_from_counters_that_went_backwards_are_left_out():
    net0 = {"primary": "eth0", "ifaces": {"eth0": {"rx": 1000, "tx": 500}}}
    net1 = {"primary": "eth0", "ifaces": {"eth0": {"rx": 3000, "tx": 900}}}
    assert net_rates(net1, net0, 2.0) == ("eth0", ["eth0"], 1000.0, 200.0)
    assert net_rates(net0, net1, 2.0)[2:] == (None, None)
    d0 = {"nvme0n1": {"read_sectors": 100, "write_sectors": 10}}
    d1 = {"nvme0n1": {"read_sectors": 4100, "write_sectors": 10}}
    assert disk_rates(d1, d0, 2.0) == (1_024_000.0, 0.0)
    assert disk_rates(d0, d1, 2.0) == (None, 0.0)
    assert disk_rates(d1, None, 2.0) == (None, None)


def test_journal_counters_difference_and_survive_an_agent_restart():
    assert counter_deltas({"auth_fail": 3}, None)["auth_fail"] == 0
    got = counter_deltas({"auth_fail": 5, "cancel": 2}, {"auth_fail": 3, "cancel": 2})
    assert got == {"auth_fail": 2, "cache_evict": None, "cache_skip": None, "cancels": 0}
    # The agent restarted and counted 1 since: that 1 is new.
    assert counter_deltas({"auth_fail": 1}, {"auth_fail": 5})["auth_fail"] == 1


def test_live_decode_ignores_stale_and_prefilling_slots():
    journal = {
        "slots": [
            {"phase": "decode", "tg_3s": 41.0},
            {"phase": "decode", "tg_3s": 55.0, "stale": True},
            {"phase": "prefill", "tg_3s": 99.0},
        ]
    }
    live, tg = live_decode(journal)
    assert len(live) == 1 and tg == 41.0
    assert live_decode({}) == ([], None)
