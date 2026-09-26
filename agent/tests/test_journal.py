"""The journal parser against real logs, one fixture per llama.cpp build.

llama.cpp's log format is not an API. Each fixture is a slice of a real
service log (three complete requests, plus the first lines of every other
pattern that build writes), with the parse it gave when it was captured. A
build that changes its log, or a parser change, shows up here first.

To add a build: `python tools/log_fixture.py <log> <name>` cuts one from a
whole log (journalctl, docker logs or plain output). See CONTRIBUTING.md.
"""

import json
import re
from pathlib import Path

import pytest
from inferenceinquire_agent.journal.parser import JournalParser

FIXTURES = sorted(Path(__file__).parent.joinpath("fixtures", "journal").glob("*.log"))


def parse(lines):
    p = JournalParser()
    for line in lines:
        p.ingest(line)
    return p.snapshot()


@pytest.mark.parametrize("log", FIXTURES, ids=lambda p: p.stem)
def test_build_log_parses_as_captured(log):
    snap = parse(log.read_text().splitlines())
    want = json.loads(log.with_suffix(".expected.json").read_text())
    assert snap["matched"] == want["matched"]
    got = [{k: r.get(k) for k in want["requests"][0]} for r in snap["requests"]]
    assert got == want["requests"]
    assert snap["counters"] == want["counters"]
    assert {k: snap["load"][k] for k in want["load"]} == want["load"]


@pytest.mark.parametrize("log", FIXTURES, ids=lambda p: p.stem)
def test_every_completed_request_has_its_timings(log):
    """What the request panels need: prefill and decode rate per request."""
    snap = parse(log.read_text().splitlines())
    done = [r for r in snap["requests"] if r.get("n_tokens")]
    assert len(done) >= 3
    for r in done:
        assert r.get("prefill_tps") and r.get("decode_tps"), r


@pytest.mark.parametrize("log", FIXTURES, ids=lambda p: p.stem)
def test_no_timing_line_goes_unread(log):
    """Every per-request timing line matches a pattern. (The prefill progress
    lines are the one kind llama.cpp writes that the agent does not use.)"""
    p = JournalParser()
    unread = []
    for line in log.read_text().splitlines():
        before = sum(p.matched.values())
        p.ingest(line)
        if (
            "print_timing" in line
            and "prompt processing" not in line
            and sum(p.matched.values()) == before
        ):
            unread.append(line)
    assert not unread, unread[:3]


def test_a_renamed_log_format_is_counted_as_unread():
    """The failure these fixtures exist for: a build renames a field, the
    parser stops matching, and nothing errors. The per-pattern counts are how
    the dashboard notices (see the request_telemetry rule)."""
    log = next(p for p in FIXTURES if "b10935" in p.name)
    renamed = [
        re.sub(r"(?<!prompt )eval time", "gen time", ln) for ln in log.read_text().splitlines()
    ]
    snap = parse(renamed)
    assert snap["matched"]["eval"] == 0
    assert snap["requests"] and not any(r.get("decode_tps") for r in snap["requests"])
