"""Reading llama.cpp: the Prometheus parser, and when /slots may be read."""

from inferenceinquire.sources import build_number, parse_prometheus, should_poll_slots

# --------------------------------------------------------------- parsing --

# A trimmed but verbatim-shaped capture of this server's /metrics.
SAMPLE_METRICS = """\
# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 177073
llamacpp:prompt_tokens_cached_total 8.56286e+06
llamacpp:tokens_predicted_total 126461
llamacpp:tokens_predicted_seconds_total 2801.67
llamacpp:requests_processing 0
llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="0"} 38421
llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position="1"} 29656
"""


def test_parse_plain_and_scientific():
    plain, _ = parse_prometheus(SAMPLE_METRICS)
    assert plain["llamacpp:prompt_tokens_total"] == 177073
    # llama.cpp emits large counters in scientific notation.
    assert plain["llamacpp:prompt_tokens_cached_total"] == 8562860.0
    assert plain["llamacpp:requests_processing"] == 0


def test_parse_labelled_series():
    _, labelled = parse_prometheus(SAMPLE_METRICS)
    pos = labelled["llamacpp:spec_decode_num_accepted_tokens_per_pos_total"]
    assert pos == {"0": 38421.0, "1": 29656.0}


def test_parse_ignores_comments_and_junk():
    plain, _ = parse_prometheus("# HELP x y\n\nnot_a_metric\nfoo 1.5\n")
    assert plain == {"foo": 1.5}


# ----------------------------------------------- sleep-on-idle ------------

BUSY = {"llamacpp:requests_processing": 1}
IDLE = {"llamacpp:requests_processing": 0, "llamacpp:requests_deferred": 0}


def test_slots_are_read_every_poll_when_the_model_never_sleeps():
    assert should_poll_slots("auto", None, {}, IDLE)
    assert should_poll_slots("auto", -1, {}, IDLE)


def test_slots_are_left_alone_while_an_idle_model_may_sleep():
    # /slots is queued like real work: reading it resets the idle timer.
    assert not should_poll_slots("auto", 300, {}, IDLE)
    assert should_poll_slots("auto", 300, {}, BUSY)
    assert should_poll_slots("auto", 300, {}, {"llamacpp:requests_deferred": 2})
    # No agent to report the flag: "busy" opts in by hand.
    assert not should_poll_slots("busy", None, {}, IDLE)
    assert should_poll_slots("busy", None, {}, BUSY)
    # Without /metrics there is no way to tell busy from idle: stay away.
    assert not should_poll_slots("auto", 300, {}, None)


def test_a_sleeping_model_is_never_woken():
    for mode in ("auto", "busy", "always"):
        assert not should_poll_slots(mode, 300, {"is_sleeping": True}, BUSY)
    assert should_poll_slots("always", 300, {"is_sleeping": False}, IDLE)


def test_build_number():
    assert build_number("b10935-8e330954a") == 10935
    assert build_number("10519") == 10519
    assert build_number("") is None and build_number(None) is None
    assert build_number("unknown") is None
