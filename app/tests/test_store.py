"""Persistence: baselines, the storage round trip, history fidelity, usage and rollups."""

from inferenceinquire import config
from inferenceinquire.schema import GPU_COLS, SAMPLE_COLS, aggregate_window
from support import make_store


def test_baseline_needs_enough_samples(tmp_path):
    st = make_store(tmp_path)
    for i in range(config.MIN_BASELINE_SAMPLES - 1):
        st.record_decode(i, "model-a", 44.0)
    assert st.decode_baseline("model-a")[0] is None


def test_baseline_is_a_median_so_outliers_do_not_drag_it(tmp_path):
    st = make_store(tmp_path)
    for i in range(20):
        st.record_decode(i, "model-a", 44.0)
    # A burst of CPU-fallback samples must not redefine "normal".
    for i in range(20, 25):
        st.record_decode(i, "model-a", 2.5)
    base, n = st.decode_baseline("model-a")
    assert base == 44.0 and n == 25


def test_baselines_are_per_model(tmp_path):
    st = make_store(tmp_path)
    for i in range(20):
        st.record_decode(i, "fast", 90.0)
        st.record_decode(i, "slow", 12.0)
    assert st.decode_baseline("fast")[0] == 90.0
    assert st.decode_baseline("slow")[0] == 12.0


# ----------------------------------------------------------- persistence --
# These exist because the storage round-trip had no coverage at all, and a
# hand-written INSERT silently desynchronised from GPU_COLS: adding
# pcie_rx/pcie_tx left 8 placeholders against 10 bindings, so every persist
# raised, GPU history stopped for ~19 hours, and the only symptom was one
# WARNING line and four blank cards.


def test_gpu_samples_round_trip_every_declared_column(tmp_path):
    st = make_store(tmp_path)
    gpu = {"idx": 0}
    for i, col in enumerate(GPU_COLS):
        gpu[col] = float(i + 1)
    st.write({"ts": 1000.0}, [gpu])

    out = st.series(since=0.0, bucket=1.0)
    assert "0" in out["gpus"], "no GPU series was written"
    got = out["gpus"]["0"]
    for i, col in enumerate(GPU_COLS):
        assert got[col] == [float(i + 1)], f"{col} did not survive the round trip"


def test_samples_round_trip_every_declared_column(tmp_path):
    st = make_store(tmp_path)
    point = {c: float(i + 1) for i, c in enumerate(SAMPLE_COLS)}
    point["ts"] = 2000.0
    st.write(point, [])

    out = st.series(since=0.0, bucket=1.0)
    assert out["samples"]["ts"] == [2000.0]
    for i, col in enumerate(SAMPLE_COLS):
        if col == "ts":
            continue
        assert out["samples"][col] == [float(i + 1)], f"{col} did not survive"


def test_write_survives_a_gpu_missing_a_metric(tmp_path):
    """A card that does not report a field must not break the whole write."""
    st = make_store(tmp_path)
    st.write({"ts": 3000.0}, [{"idx": 0}])  # no metrics at all
    out = st.series(since=0.0, bucket=1.0)
    assert out["gpus"]["0"]["ts"] == [3000.0]


# ------------------------------------------------------ history fidelity --


def test_window_keeps_peaks_and_every_observation():
    # Five polls: one 330 W prefill spike, one completed request.
    window = []
    for i, (w, dec) in enumerate([(90, None), (330, None), (95, 52.0), (92, None), (91, None)]):
        window.append(
            (
                {
                    "ts": float(i),
                    "decode_tps": dec,
                    "req_proc": 1 if i == 1 else 0,
                    "auth_fail": 1,
                    "healthy": 1,
                },
                [{"idx": 0, "pwr": w, "hbm": 60 + i, "temp": 50}],
            )
        )
    point, gpus = aggregate_window(window)
    assert point["ts"] == 4.0
    # Taking the last poll alone dropped this request - 4 of 5 were lost.
    assert point["decode_tps"] == 52.0
    assert point["req_proc"] == 1 and point["auth_fail"] == 5
    g = gpus[0]
    assert g["pwr_max"] == 330 and abs(g["pwr"] - 139.6) < 1e-9 and g["hbm_max"] == 64


def test_long_range_buckets_keep_the_peak(tmp_path):
    st = make_store(tmp_path)
    st.write({"ts": 100.0}, [{"idx": 0, "pwr": 100.0, "pwr_max": 330.0}])
    st.write({"ts": 110.0}, [{"idx": 0, "pwr": 120.0, "pwr_max": 140.0}])
    # Rows written before peaks were stored still count as their own peak.
    st.write({"ts": 120.0}, [{"idx": 0, "pwr": 150.0}])
    g = st.series(since=0.0, bucket=1000.0)["gpus"]["0"]
    assert g["pwr_max"] == [330.0] and abs(g["pwr"][0] - 123.333) < 0.01


# ---------------------------------------------------- energy and usage -----


def _usage_store(tmp_path):
    st = make_store(tmp_path)
    t0 = 1_000_000.0
    # One hour of rows every 10 s: two cards at 100 W and 50 W, busy for the
    # first half hour at 300 + 250 W. Then a 20-minute hole (dashboard down),
    # then 10 more minutes idle.
    ts = t0
    while ts < t0 + 3600:
        busy = ts < t0 + 1800
        st.write(
            {"ts": ts, "req_proc": 1 if busy else 0},
            [
                {"idx": 0, "pwr": 300.0 if busy else 100.0},
                {"idx": 1, "pwr": 250.0 if busy else 50.0},
            ],
        )
        ts += 10
    ts = t0 + 3600 + 1200
    while ts < t0 + 3600 + 1200 + 600:
        st.write({"ts": ts, "req_proc": 0}, [{"idx": 0, "pwr": 100.0}, {"idx": 1, "pwr": 50.0}])
        ts += 10
    st.record_request(
        {
            "task": 1,
            "slot": 0,
            "finished": t0 + 100,
            "gen_tokens": 2000,
            "prompt_tokens": 5000,
            "decode_tps": 50.0,
        },
        "m",
    )
    st.record_request(
        {
            "task": 2,
            "slot": 3,
            "finished": t0 + 200,
            "gen_tokens": 48,
            "prompt_tokens": 4,
            "n_tokens": 73,
            "decode_tps": 70.0,
        },
        "m",
    )
    st.conn.execute("UPDATE requests SET probe = 1 WHERE task = 2")
    st.conn.commit()
    return st, t0


def test_usage_integrates_energy_and_treats_a_gap_as_missing(tmp_path):
    st, t0 = _usage_store(tmp_path)
    u = st.usage(t0, t0 + 7200, 3600)
    tot = u["totals"]
    # Busy half hour 550 W, idle half hour 150 W, idle 10 min 150 W. The
    # 20-minute hole is capped at 3 windows (30 s), never 20 min at 150 W.
    expect = 550 * 1800 / 3600 + 150 * 1800 / 3600 + 150 * (600 + 30 - 10) / 3600
    assert abs(tot["gpu_wh"] - expect) / expect < 0.02
    assert abs(tot["busy_wh"] - 550 * 1800 / 3600) / (550 * 1800 / 3600) < 0.02
    assert tot["idle_w"] == 150.0
    assert 0.35 < tot["busy_share"] < 0.5
    assert tot["gen_wl"] == 2000 and tot["gen_probe"] == 48 and tot["req_wl"] == 1
    assert abs(tot["wh_per_1k_gen"] - tot["gpu_wh"] / 2.048) < 1e-9
    assert [b["ts"] for b in u["buckets"]] == [t0 - (t0 % 3600) + k * 3600 for k in range(3)][
        : len(u["buckets"])
    ]
    assert "cost" not in tot  # no price configured


def test_usage_costs_when_a_price_is_set(tmp_path, monkeypatch):
    st, t0 = _usage_store(tmp_path)
    monkeypatch.setattr(config, "ENERGY_PRICE_PER_KWH", 0.30)
    tot = st.usage(t0, t0 + 7200, 3600)["totals"]
    assert abs(tot["cost"] - tot["gpu_wh"] / 1000 * 0.30) < 1e-9 and tot["cost_basis"] == "gpu"


# ------------------------------------------------ long-term rollups -------


def _raw_history(st, t0, seconds, spike_at=None):
    ts = t0
    while ts < t0 + seconds:
        pwr = 400.0 if spike_at is not None and abs(ts - spike_at) < 5 else 100.0
        st.write(
            {
                "ts": ts,
                "req_proc": 1 if int(ts) % 600 < 60 else 0,
                "auth_fail": 1,
                "decode_tps": 50.0,
                "healthy": 1,
            },
            [
                {"idx": 0, "pwr": pwr, "pwr_max": pwr, "temp": 40.0},
                {"idx": 1, "pwr": 90.0, "pwr_max": 95.0, "temp": 41.0},
            ],
        )
        ts += 10


def test_rollup_backfills_keeps_peaks_and_sums_and_is_incremental(tmp_path):
    st = make_store(tmp_path)
    t0 = 1_800_000_000.0  # on an hour boundary
    _raw_history(st, t0, 7200, spike_at=t0 + 1234)
    st.rollup(t0 + 7200 + 60)
    n5 = st.conn.execute("SELECT COUNT(*) FROM samples_5m").fetchone()[0]
    n1 = st.conn.execute("SELECT COUNT(*) FROM samples_1h").fetchone()[0]
    assert (n5, n1) == (24, 2)
    # The one-sample spike survives as the 5-minute and hourly peak.
    b = t0 + 1200
    assert (
        st.conn.execute(
            "SELECT pwr_max FROM gpu_samples_5m WHERE ts = ? AND idx = 0", (b,)
        ).fetchone()[0]
        == 400.0
    )
    assert (
        st.conn.execute(
            "SELECT pwr_max FROM gpu_samples_1h WHERE ts = ? AND idx = 0", (t0,)
        ).fetchone()[0]
        == 400.0
    )
    # Counts add up (30 raw rows of auth_fail=1 per 5 minutes); means stay means.
    s5 = st.conn.execute(
        "SELECT auth_fail, decode_tps FROM samples_5m WHERE ts = ?", (b,)
    ).fetchone()
    assert s5 == (30.0, 50.0)
    # Incremental and idempotent: a second run over nothing new changes nothing,
    # and new raw rows extend the rollups from the cursor.
    st.rollup(t0 + 7200 + 60)
    assert st.conn.execute("SELECT COUNT(*) FROM samples_5m").fetchone()[0] == 24
    _raw_history(st, t0 + 7200, 1800)
    st.rollup(t0 + 9000 + 60)
    assert st.conn.execute("SELECT COUNT(*) FROM samples_5m").fetchone()[0] == 30


def test_requests_fold_per_hour_and_model(tmp_path):
    st = make_store(tmp_path)
    t0 = 1_800_000_000.0
    _raw_history(st, t0, 3600)
    for i, (gen, tps, probe) in enumerate([(500, 40.0, 0), (1500, 60.0, 0), (48, 70.0, 1)]):
        st.record_request(
            {
                "task": i,
                "slot": 0,
                "finished": t0 + 100 + i,
                "gen_tokens": gen,
                "prompt_tokens": 20000 if i == 1 else 10,
                "prefill_tps": 900.0,
                "decode_tps": tps,
            },
            "m.gguf",
        )
        if probe:
            st.conn.execute("UPDATE requests SET probe = 1 WHERE task = ?", (i,))
    st.conn.commit()
    st.rollup(t0 + 3600 + 60)
    r = st.conn.execute(
        "SELECT model, n_wl, n_probe, gen_wl, decode_wl, decode_probe, "
        "prefill_wl, cold_n FROM requests_1h"
    ).fetchone()
    assert r == ("m.gguf", 2.0, 1.0, 2000.0, 50.0, 70.0, 900.0, 1.0)


def test_long_ranges_read_the_rollups(tmp_path, monkeypatch):
    import time as _time

    st = make_store(tmp_path)
    now = _time.time()
    t0 = now - 6 * 3600
    _raw_history(st, t0, 6 * 3600 - 600, spike_at=t0 + 3000)
    st.rollup(now)
    monkeypatch.setattr(config, "RETAIN_DAYS", 0.1)  # 2.4 h of raw: 6 h is "long"
    ser = st.series(t0, 1800)
    assert ser["source"] == "5m" and ser["requests"] is not None
    assert max(v for v in ser["gpus"]["0"]["pwr_max"] if v is not None) == 400.0
    u = st.usage(t0, now, 3600)
    assert u["source"] == "5m" and u["totals"]["gpu_wh"] > 0


def test_a_new_database_says_so_once_and_an_old_one_names_its_migrations(tmp_path, caplog):
    import logging
    import sqlite3

    from inferenceinquire.store import Store

    caplog.set_level(logging.INFO, logger="inferenceinquire.store")
    Store(str(tmp_path / "new.db"))
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [f"created a new database at {tmp_path / 'new.db'}"]

    old = tmp_path / "old.db"
    sqlite3.connect(old).execute("CREATE TABLE samples (ts REAL PRIMARY KEY)").connection.commit()
    caplog.clear()
    Store(str(old))
    migrated = [r.getMessage() for r in caplog.records if r.getMessage().startswith("migrated")]
    assert "migrated: samples.decode_tps added" in migrated
    assert not [m for m in migrated if "samples_5m" in m]  # created now, not migrated
