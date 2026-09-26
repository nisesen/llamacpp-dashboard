"""Telling a health-check probe from real work."""

import pytest
from inferenceinquire import config
from inferenceinquire.store import Store
from inferenceinquire.workload import is_probe, request_view
from support import make_store


@pytest.fixture
def probe48(monkeypatch):
    monkeypatch.setattr(config, "PROBE_GEN_TOKENS", 48)
    monkeypatch.setattr(config, "PROBE_MAX_TOKENS", 256)


def test_no_probe_handling_unless_configured(monkeypatch):
    monkeypatch.setattr(config, "PROBE_GEN_TOKENS", None)
    assert not is_probe({"gen_tokens": 48, "n_tokens": 73})


def test_unconfigured_store_marks_nothing_as_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROBE_GEN_TOKENS", None)
    st = make_store(tmp_path)
    st.record_request({"task": 1, "finished": 10.0, "gen_tokens": 48, "n_tokens": 73}, "m")
    assert [r["probe"] for r in st.requests_since(0)] == [0]


def test_probe_is_recognised_exactly(probe48):
    assert is_probe({"gen_tokens": 48, "n_tokens": 73})
    # Real traffic that happens to generate 48 tokens has a real prompt.
    assert not is_probe({"gen_tokens": 48, "n_tokens": 14_000})
    assert not is_probe({"gen_tokens": 40, "n_tokens": 62})
    assert not is_probe({"gen_tokens": None, "n_tokens": None})


def test_stored_requests_are_classified_and_backfilled(tmp_path, probe48):
    st = make_store(tmp_path)
    st.record_request({"task": 1, "finished": 10.0, "gen_tokens": 48, "n_tokens": 73}, "m")
    st.record_request({"task": 2, "finished": 11.0, "gen_tokens": 900, "n_tokens": 50_000}, "m")
    got = {r["task"]: r["probe"] for r in st.requests_since(0)}
    assert got == {1: 1, 2: 0}
    # Rows from before the column existed are classified on the next start.
    st.conn.execute("UPDATE requests SET probe = NULL")
    st.conn.commit()
    st2 = Store(st.path)
    assert {r["task"]: r["probe"] for r in st2.requests_since(0)} == {1: 1, 2: 0}


def test_request_view_reuse_and_cold_prefill():
    warm = request_view(
        {"n_tokens": 50_900, "gen_tokens": 900, "prompt_tokens": 500, "prefill_tps": 900.0}
    )
    assert abs(warm["reuse"] - 0.99) < 1e-9 and not warm["cold"]
    cold = request_view(
        {"n_tokens": 87_100, "gen_tokens": 50, "prompt_tokens": 87_043, "prefill_tps": 1056.0}
    )
    assert cold["cold"] and cold["prefill_tps"] == 1056.0
    # A 4-token tail after a cache hit is not a prefill rate worth showing.
    tail = request_view({"n_tokens": 73, "gen_tokens": 48, "prompt_tokens": 4, "prefill_tps": 19.0})
    assert tail["prefill_tps"] is None


def test_request_stats_split_workload_from_probe(tmp_path, probe48):
    st = make_store(tmp_path)
    now = 1_000_000.0
    for i in range(5):
        st.record_request(
            {
                "task": i,
                "finished": now - 100 - i,
                "gen_tokens": 48,
                "n_tokens": 73,
                "decode_tps": 70.0,
            },
            "m",
        )
    for i in range(5, 8):
        st.record_request(
            {
                "task": i,
                "finished": now - 50 - i,
                "gen_tokens": 500,
                "n_tokens": 60_000,
                "decode_tps": 52.0,
                "prompt_tokens": 20_000,
                "prefill_tps": 1000.0,
            },
            "m",
        )
    s = st.request_stats("m", now)
    assert s["probe_n"] == 5 and s["workload_n"] == 3
    assert s["probe_decode_med"] == 70.0 and s["workload_decode_med"] == 52.0
    assert s["cold_n"] == 3 and abs(s["cold_seconds"] - 60.0) < 1e-9
