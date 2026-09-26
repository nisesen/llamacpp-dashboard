"""Checks the agent runs on itself: `inferenceinquire-agent --self-test`.

The journal patterns are the riskiest part of the agent: llama.cpp's log
format is not an API, so a build change can silently stop the live panels
without anything erroring. The installer runs these checks before it replaces
a working agent, and CI runs each one as a test. Run them after any llama.cpp
upgrade.

A check raises AssertionError when it fails and returns what it proved.
"""

import os
import sys
import tempfile
import threading
import time

from .cache import Cache
from .collectors.gguf import arch_from_meta
from .collectors.ipmi import parse_sel
from .collectors.nvidia import parse_gpu_line
from .journal.follower import JournalFollower
from .journal.parser import JournalParser
from .journal.patterns import (
    RE_DRAFT,
    RE_EVAL,
    RE_LAUNCH,
    RE_PICK_LCP,
    RE_PICK_LRU,
    RE_PROMPT,
    RE_RELEASE,
    RE_TG,
)
from .runtimes.llama_args import EXEC_CACHE_RAM, EXEC_SLEEP
from .runtimes.proxmox_lxc import conf_value
from .runtimes.streams import ProcessStream

SELF_TEST_LINES = [
    # Real capture, in the order llama.cpp actually emits it: the slot is
    # picked, launched, reports progress, then its timings, then is released.
    (
        "1789626154.2 llm llama-server[1]: 533.06 I slot get_availabl: id  1 | task -1 | "
        "selected slot by LCP similarity, f_sim_best = 0.630 (> 0.100 thold), f_keep = 0.127",
        "lcp",
    ),
    (
        "1789626154.2 llm llama-server[1]: 533.06 I slot launch_slot_: id  1 | task 68551 | "
        "processing task, is_child = 0",
        "launch",
    ),
    (
        "1789626154.2 llm llama-server[1]: 533.06 I slot print_timing: id  0 | task 173893 | "
        "n_gen =    374, tg =  30.43 t/s, tg_3s =  23.96 t/s",
        "tg",
    ),
    (
        "1789626154.2 llm llama-server[1]: 533.06 I slot print_timing: id  1 | task 68551 | "
        "prompt eval time =     896.79 ms /    81 tokens (   11.07 ms per token,    90.32 tokens per second)",  # noqa: E501
        "prompt",
    ),
    (
        "1789626154.2 llm llama-server[1]: 533.06 I slot print_timing: id  1 | task 68551 | "
        "       eval time =   11562.33 ms /   320 tokens (   36.25 ms per token,    27.59 tokens per second)",  # noqa: E501
        "eval",
    ),
    (
        "1789626154.2 llm llama-server[1]: 533.06 I slot print_timing: id  1 | task 68551 | "
        "draft acceptance = 0.73256 (  189 accepted /   258 generated), mean len =  2.47",
        "draft",
    ),
    (
        "1789626154.2 llm llama-server[1]: 533.06 I slot      release: id  1 | task 68551 | "
        "stop processing: n_tokens = 400, truncated = 0",
        "release",
    ),
    (
        "1789626154.2 llm llama-server[1]: 533.06 I slot get_availabl: id  2 | task -1 | "
        "selected slot by LRU, t_last = 80267091579",
        "lru",
    ),
]


def check_journal_patterns():
    checks = [
        ("tg", RE_TG, lambda m: m.group(5) == "23.96"),
        ("prompt", RE_PROMPT, lambda m: m.group(6) == "90.32"),
        ("eval", RE_EVAL, lambda m: m.group(6) == "27.59"),
        ("draft", RE_DRAFT, lambda m: m.group(4) == "189" and m.group(6) == "2.47"),
        ("launch", RE_LAUNCH, lambda m: m.group(2) == "68551"),
        ("release", RE_RELEASE, lambda m: m.group(3) == "400"),
        ("lcp", RE_PICK_LCP, lambda m: m.group(2) == "0.630"),
        ("lru", RE_PICK_LRU, lambda m: m.group(1) == "2"),
    ]
    by_tag = {tag: line for line, tag in SELF_TEST_LINES}
    for tag, rx, check in checks:
        m = rx.search(by_tag[tag])
        assert m, f"{tag}: no match"
        assert check(m), f"{tag}: matched but wrong groups: {m.groups()}"
    return f"journal patterns: {', '.join(tag for tag, _, _ in checks)}"


def check_request_assembly():
    """The parser must turn those lines into one coherent request."""
    p = JournalParser()
    for line, _ in SELF_TEST_LINES:
        p.ingest(line)
    snap = p.snapshot()
    assert snap["requests"], "no completed request assembled"
    r = snap["requests"][0]
    for key, want in (
        ("prompt_tokens", 81),
        ("gen_tokens", 320),
        ("accepted", 189),
        ("n_tokens", 400),
    ):
        assert r.get(key) == want, f"request.{key}: {r.get(key)} != {want}"
    assert r.get("pick", {}).get("how") == "cache", f"slot pick not carried: {r.get('pick')}"
    return "request assembled: " + str(
        {k: r.get(k) for k in ("prompt_tokens", "gen_tokens", "accept_rate", "pick")}
    )


def check_watchdog_kill():
    """The watchdog must end a silent tail without deadlocking.

    The failure: the watchdog closed the pipe while run() was blocked reading
    it, and waited forever on the reader's lock. A stand-in writer that never
    writes plays the stopped unit's tail.
    """
    f = JournalFollower()
    f.stream = ProcessStream([sys.executable, "-c", "import time; time.sleep(60)"])
    got_eof = threading.Event()

    def reader():
        for _ in f.stream:
            pass
        got_eof.set()

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.3)  # let the reader block
    done = threading.Event()
    threading.Thread(target=lambda: (f._kill(close_pipe=False), done.set()), daemon=True).start()
    assert done.wait(8) and got_eof.wait(8), (
        f"kill returned={done.is_set()} reader_eof={got_eof.is_set()}"
    )
    f._kill()  # the reader's own cleanup
    return "watchdog ends a silent tail: kill returns, reader sees EOF"


def check_keyed_cache():
    """A keyed cache goes stale when its key changes, not only on TTL."""
    calls, key = [], ["a.gguf"]
    c = Cache(lambda: calls.append(1) or len(calls), 900.0, key=lambda: key[0])
    c.refresh()
    fresh = not c.stale()
    key[0] = "b.gguf"
    assert fresh and c.stale(), f"fresh={fresh} stale-after-change={c.stale()}"
    return "keyed cache: a model switch makes the anatomy stale at once"


def check_log_counters():
    """Log signals /metrics does not carry, from real captured lines. Each is
    counted once, and a replay of the same lines - what every tail respawn
    does - must not count them again."""
    signal_lines = [
        "1789541656.127692 llm llama-server[2322]: 357.23.741.722 W srv    "
        "operator(): unauthorized: Invalid API Key",
        "1789541656.127801 llm llama-server[2322]: 357.23.741.800 W srv    "
        "operator(): unauthorized: Invalid API Key",
        "1789545753.447239 llm llama-server[3476]: 12.08.750.957 W srv         alloc:  - "
        "prompt state size 8658.373 MiB exceeds cache size limit 8192.000 MiB, skipping",
        "1789545800.451401 llm llama-server[3476]: 3.12.065.597 W srv         alloc:  - "
        "making room for prompt cache entry, removing oldest entry (size = 465.405 MiB)",
        "1789545842.282047 llm llama-server[3476]: 13.37.585.585 W srv          "
        "stop: cancel task, id_task = 5136",
    ]
    p = JournalParser()
    for line in signal_lines + signal_lines:  # the second pass is a replay
        p.ingest(line)
    got = p.snapshot()["counters"]
    want = {"auth_fail": 2, "cache_evict": 1, "cache_skip": 1, "cancel": 1}
    assert all(got.get(k) == v for k, v in want.items()), f"{got} != {want}"
    assert abs(got.get("cache_evict_mib", 0) - 465.405) <= 0.01, got
    limit = p.snapshot()["cache_limit_mib"]
    return f"log counters: { ({k: got[k] for k in want}) } limit {limit} MiB"


def check_sel_parse():
    """Verbatim BMC log shape. The junk records carry the HIGHEST ids, which is
    how "last 12 by id" came to show nothing but junk."""
    sel = parse_sel(
        "   2 | 07/02/2025 | 06:50:53 PM PDT | Fan FANB | Lower Critical going low  "
        "| Asserted | Reading 300 < Threshold 500 RPM\n"
        "   9 | 01/14/2026 | 03:10:02 AM PST | Memory | Uncorrectable ECC (@DIMMG1(CPU1)) "
        "| Asserted\n"
        "  3f | 11/17/2019 | 09:33:22 PM PST | Unknown #0xff |  | Asserted\n"
    )
    real = [e for e in sel if not e["noise"]]
    assert [e["rid"] for e in sel] == [2, 9, 63], sel
    assert len(real) == 2 and real[-1]["ts"] is not None and "DIMMG1" in real[-1]["what"], sel
    return (
        f"sel parse: {len(real)} real, {len(sel) - len(real)} noise, "
        f"newest real id {real[-1]['rid']}"
    )


def check_cache_ram_arg():
    """The serving unit's prompt-cache limit, read from its command line."""
    for argv, want in (
        ("llama-server --model m.gguf --cache-ram 24576 --port 8080", "24576"),
        ("llama-server -cram -1 --port 8080", "-1"),
        ("llama-server --model m.gguf --port 8080", None),
    ):
        m = EXEC_CACHE_RAM.search(argv)
        got = m.group(1) if m else None
        assert got == want, f"{argv!r} -> {got}, want {want}"
    return "cache-ram parse: set, unlimited, and default"


def check_sleep_arg():
    """The serving unit's sleep-on-idle setting, read from its command line."""
    for argv, want in (
        ("llama-server -m m.gguf --sleep-idle-seconds 300 --port 8080", "300"),
        ("llama-server -m m.gguf --sleep-idle-seconds -1", "-1"),
        ("llama-server -m m.gguf --port 8080", None),
    ):
        m = EXEC_SLEEP.search(argv)
        got = m.group(1) if m else None
        assert got == want, f"{argv!r} -> {got}, want {want}"
    return "sleep-idle parse: set, disabled, and absent"


def check_gpu_row():
    """GPU rows: the one parser both the stream and one-shot queries use."""
    row = (
        "0, NVIDIA A100-SXM4-40GB, 1564020000000, GPU-abc, 00000000:41:00.0, 31, 33, "
        "12, 3, 26400, 40960, 89.51, 250.00, 250.00, 1095, 1215, 1410, P0, 4, 4, 16, "
        "16, 0, 0, 0, 0, No, No, Active, Not Active, Not Active, Not Active, "
        "Not Active, Enabled, [N/A]"
    )
    g = parse_gpu_line(row)
    assert g and g["index"] == 0 and g["power.draw"] == 89.51, g
    assert g["fan.speed"] is None and g["throttle"] == ["power cap"], g
    assert parse_gpu_line("0, 31, 89.5") is None, "a short row was accepted"
    return "gpu row parse: numbers, N/A and throttle flags; short rows rejected"


def check_guest_conf():
    """Guest configs: a snapshot section repeats keys with old values."""
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as tf:
        tf.write(
            "arch: amd64\nhostname: llm\nmemory: 4096\n\n[before-upgrade]\nhostname: old-name\n"
        )
    try:
        got = (
            conf_value(tf.name, "hostname"),
            conf_value(tf.name, "name"),
            conf_value("/nonexistent/100.conf", "hostname"),
        )
    finally:
        os.unlink(tf.name)
    assert got == ("llm", "", ""), got
    return "guest conf parse: live section only, missing key and file are empty"


def check_arch_shapes():
    """The box serves whatever is loaded, so the anatomy panel has to cope with
    more than the model that happens to be running."""
    shapes = {
        "sparse hybrid GQA": (
            {
                "general.architecture": "qwen4exp",
                "qwen4exp.block_count": 48,
                "qwen4exp.attention.head_count": 24,
                "qwen4exp.attention.head_count_kv": 2,
                "qwen4exp.expert_count": 512,
                "qwen4exp.expert_used_count": 10,
                "qwen4exp.full_attention_interval": 4,
                "qwen4exp.attention.compress_ratios": [0, 0, 0, 4] * 12,
            },
            (48, 12, 512, 2, True),
        ),
        "dense, interval only": (
            {
                "general.architecture": "qwen35",
                "qwen35.block_count": 65,
                "qwen35.attention.head_count": 24,
                "qwen35.attention.head_count_kv": 4,
                "qwen35.full_attention_interval": 4,
            },
            (65, 16, None, 4, True),
        ),
        "dense, uniform, MHA": (
            {
                "general.architecture": "gemma9",
                "gemma9.block_count": 42,
                "gemma9.attention.head_count": 16,
            },
            (42, 0, None, 16, False),
        ),
    }
    for name, (meta, want) in shapes.items():
        a = arch_from_meta(meta, "/x/y.gguf")
        got = (
            a["n_layer"],
            len(a["full_attention_layers"]),
            a["expert_count"],
            a["n_head_kv"],
            a["gqa_reported"],
        )
        assert got == want, f"{name}: {got} != {want}"
    return f"architecture mapping: {', '.join(shapes)}"


CHECKS = (
    check_journal_patterns,
    check_request_assembly,
    check_watchdog_kill,
    check_keyed_cache,
    check_log_counters,
    check_sel_parse,
    check_cache_ram_arg,
    check_sleep_arg,
    check_gpu_row,
    check_guest_conf,
    check_arch_shapes,
)


def self_test() -> int:
    ok = True
    for check in CHECKS:
        try:
            print(f"ok   {check()}")
        except Exception as exc:  # a failed assertion, or a check that crashed
            print(f"FAIL {check.__name__}: {exc!r}")
            ok = False
    print("SELF-TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1
