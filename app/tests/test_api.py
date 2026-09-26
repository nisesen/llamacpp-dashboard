"""The HTTP API and what the page loads."""

from inferenceinquire import api, config
from support import make_store


def test_alerts_endpoint_returns_push_worthy_edges(tmp_path, monkeypatch):
    import asyncio
    import json as _json

    st = make_store(tmp_path)
    monkeypatch.setattr(api, "store", st)
    monkeypatch.setattr(config, "PUSH_SKIP_KEYS", {"llama", "llama_health", "units"})
    st.log_event(100.0, "critical", "gpu_count", "A GPU has disappeared", "1 of 2", "raised")
    st.log_event(101.0, "critical", "llama", "Inference server unreachable", "", "raised")
    st.log_event(102.0, "warning", "disk:local", "Storage 91% full", "", "raised")
    st.log_event(103.0, "info", "transition", "Server restarting or loading", "", "raised")
    body = _json.loads(asyncio.run(api.api_alerts(since=99.0)).body)
    assert [e["key"] for e in body["events"]] == ["gpu_count"]
    assert _json.loads(asyncio.run(api.api_alerts(since=100.0)).body)["events"] == []
    assert "poll_age" in body and "alerts" in body


def test_requests_since_returns_only_newer_rows(tmp_path, monkeypatch):
    import asyncio
    import json as _json
    import time as _time

    st = make_store(tmp_path)
    monkeypatch.setattr(api, "store", st)
    now = _time.time()
    for i, age in enumerate((3000, 200, 50)):
        st.record_request(
            {"task": i, "slot": 0, "finished": now - age, "gen_tokens": 100, "decode_tps": 50.0},
            "m",
        )
    full = _json.loads(asyncio.run(api.api_requests(range="1h")).body)
    assert [r["task"] for r in full] == [2, 1, 0]
    newer = _json.loads(asyncio.run(api.api_requests(range="1h", since=now - 100)).body)
    assert [r["task"] for r in newer] == [2]
    # `since` never reaches back past the range itself.
    old = _json.loads(asyncio.run(api.api_requests(range="5m", since=now - 99999)).body)
    assert [r["task"] for r in old] == [2, 1]


# ------------------------------------------------ event markers -----------


def test_events_since_feeds_the_chart_markers_in_time_order(tmp_path, monkeypatch):
    import asyncio
    import json as _json

    st = make_store(tmp_path)
    monkeypatch.setattr(api, "store", st)
    st.log_event(100.0, "info", "transition", "Server restarting or loading", "loading", "raised")
    st.log_event(130.0, "warning", "model_change", "Model changed", "a.gguf -> b.gguf", "raised")
    st.log_event(
        150.0, "info", "transition", "Server restarting or loading", "back after 50s", "cleared"
    )
    got = _json.loads(asyncio.run(api.api_events(since=99.0, limit=5000)).body)
    assert [(e["key"], e["state"]) for e in got] == [
        ("transition", "raised"),
        ("model_change", "raised"),
        ("transition", "cleared"),
    ]
    assert [
        e["ts"] for e in _json.loads(asyncio.run(api.api_events(since=120.0, limit=5000)).body)
    ] == [130.0, 150.0]
    # Without `since` it stays the newest-first log the event table shows.
    assert _json.loads(asyncio.run(api.api_events(limit=2)).body)[0]["ts"] == 150.0


# ------------------------------------------------------- installable -------


def test_manifest_makes_the_page_installable():
    import asyncio
    import json as _json

    r = asyncio.run(api.manifest())
    assert r.media_type == "application/manifest+json"
    m = _json.loads(r.body)
    assert m["display"] == "standalone" and m["start_url"] == "/"
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for i in m["icons"]:  # every icon it names ships
        assert (api.STATIC_DIR / i["src"].removeprefix("/static/")).is_file()


# ---------------------------------------------- a pinned window -----------


def test_a_pinned_end_bounds_every_window(tmp_path, monkeypatch):
    import asyncio
    import json as _json
    import time as _time

    st = make_store(tmp_path)
    monkeypatch.setattr(api, "store", st)
    now = _time.time()
    for i, age in enumerate((7200, 3000, 600)):
        st.record_request(
            {"task": i, "slot": 0, "finished": now - age, "gen_tokens": 100, "decode_tps": 50.0},
            "m",
        )
        st.write({"ts": now - age, "req_proc": 1}, [{"idx": 0, "pwr": 100.0 + i}])
    end = now - 2000  # a moment between the 2nd and 3rd
    got = _json.loads(asyncio.run(api.api_requests(range="6h", end=end)).body)
    assert [r["task"] for r in got] == [1, 0]
    ser = _json.loads(asyncio.run(api.api_series(range="6h", end=end)).body)
    assert max(ser["samples"]["ts"]) < end and len(ser["gpus"]["0"]["ts"]) == 2
    # An end in the future is clamped to now; no end means now.
    assert len(_json.loads(asyncio.run(api.api_requests(range="6h", end=now + 9e6)).body)) == 3


# ------------------------------------------------ vendored charts ---------


def test_every_static_file_the_page_loads_ships():
    """The charts come from a vendored uPlot, not a CDN: the page must name
    nothing the image does not carry, and nothing off-box."""
    import re

    html = (api.STATIC_DIR / "index.html").read_text()
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert any(r.endswith("uPlot.iife.min.js") for r in refs)
    for r in refs:
        assert (api.STATIC_DIR / r.removeprefix("/static/")).is_file(), r
    assert not re.search(r'(?:src|href)="https?://', html)
    assert "MIT" in (api.STATIC_DIR / "vendor/uplot/LICENSE").read_text()


def test_every_module_the_page_imports_ships():
    """The page is ES modules with no build step: a module that imports a file
    the image does not carry breaks the page with nothing but a console error."""
    import re

    html = (api.STATIC_DIR / "index.html").read_text()
    todo = [
        api.STATIC_DIR / m.removeprefix("/static/")
        for m in re.findall(r'<script type="module" src="(/static/[^"]+)"', html)
    ]
    assert todo, "the page loads no module"
    seen = set()
    while todo:
        path = todo.pop().resolve()
        if path in seen:
            continue
        seen.add(path)
        assert path.is_file(), path
        for spec in re.findall(
            r"^\s*(?:import|export)\b[^'\"]*?from\s+'([^']+)'", path.read_text(), re.M
        ):
            assert spec.startswith("."), f"{path.name} imports {spec}: only relative paths ship"
            todo.append(path.parent / spec)
    assert len(seen) >= 10


def test_metrics_export_the_journal_pattern_counts(monkeypatch):
    import asyncio

    monkeypatch.setattr(api.monitor, "journal_matched", {"eval": 41, "release": 42})
    text = asyncio.run(api.metrics())
    assert "# TYPE llmdash_journal_lines_matched_total counter" in text
    assert 'llmdash_journal_lines_matched_total{pattern="eval"} 41' in text
    assert 'llmdash_journal_lines_matched_total{pattern="release"} 42' in text
