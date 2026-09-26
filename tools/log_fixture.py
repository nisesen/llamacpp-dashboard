"""Cut a journal fixture for a llama.cpp build from a whole log.

  python tools/log_fixture.py <log> <name>     e.g. llama-server-b11176

<log> is llama-server's output as any of:
  journalctl -u <unit> -o short-unix --no-pager > log.txt
  docker logs --timestamps <container> > log.txt 2>&1
  llama-server ... 2> log.txt            (no timestamps: they are made up)

It keeps the last three complete requests and the first line of every other
pattern the parser knows, in their original order, writes them to
agent/tests/fixtures/journal/<name>.log, and what the parser makes of them to
<name>.expected.json. agent/tests/test_journal.py then checks every future
parser against it. Read the .log before you commit it: it holds model paths.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from inferenceinquire_agent.journal.parser import JournalParser  # noqa: E402
from inferenceinquire_agent.runtimes.docker import STAMP, epoch  # noqa: E402

OUT = ROOT / "agent/tests/fixtures/journal"
SHORT_UNIX = re.compile(r"^(\d+\.\d+) \S+ ([^:]+): (.*)$")
TASK = re.compile(r"\| task\s+(-?\d+) \|")
REQUEST_KEYS = (
    "task",
    "slot",
    "prompt_tokens",
    "prefill_tps",
    "gen_tokens",
    "decode_tps",
    "accept_rate",
    "n_tokens",
    "wall_s",
    "pick",
)


def journal_lines(text: str) -> list[str]:
    """Every line in short-unix shape, with the host name taken out."""
    out, t = [], 1.8e9
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = SHORT_UNIX.match(raw)
        if m:
            out.append(f"{m.group(1)} host {m.group(2)}: {m.group(3)}")
            continue
        s = STAMP.match(raw)
        if s:
            out.append(f"{epoch(raw):.6f} host llama-server[1]: {raw[s.end() :].lstrip()}")
            continue
        t += 0.001
        out.append(f"{t:.6f} host llama-server[1]: {raw}")
    return out


def pattern_of(line: str) -> str | None:
    for name, rx, _ in JournalParser.PATTERNS:
        if rx.search(line):
            return name
    return None


def cut(lines: list[str], requests: int = 3) -> list[str]:
    parser = JournalParser()
    for line in lines:
        parser.ingest(line)
    done = [r["task"] for r in parser.snapshot()["requests"] if r.get("finished")]
    keep_tasks = set(done[:requests])  # the snapshot lists the newest first
    keep = set()
    for i, line in enumerate(lines):
        m = TASK.search(line)
        if m and int(m.group(1)) in keep_tasks:
            keep.add(i)
            # The slot choice is logged just before the launch, as task -1.
            if (
                pattern_of(line) == "launch"
                and i
                and pattern_of(lines[i - 1])
                in (
                    "pick_lcp",
                    "pick_lru",
                )
            ):
                keep.add(i - 1)
    seen = {pattern_of(lines[i]) for i in keep}
    for i, line in enumerate(lines):
        name = pattern_of(line)
        if name and name not in seen:
            keep.add(i)
            seen.add(name)
    return [lines[i] for i in sorted(keep)]


def expected(lines: list[str]) -> dict:
    parser = JournalParser()
    for line in lines:
        parser.ingest(line)
    snap = parser.snapshot()
    return {
        "matched": snap["matched"],
        "requests": [{k: r.get(k) for k in REQUEST_KEYS} for r in snap["requests"]],
        "counters": snap["counters"],
        "load": {k: snap["load"][k] for k in ("state", "path")},
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    lines = cut(journal_lines(Path(sys.argv[1]).read_text(errors="replace")))
    want = expected(lines)
    if not want["requests"]:
        print("no complete request in that log: send one through the server first")
        return 1
    log = OUT / f"{sys.argv[2]}.log"
    log.write_text("\n".join(lines) + "\n")
    log.with_suffix(".expected.json").write_text(json.dumps(want, indent=1) + "\n")
    print(f"{log}: {len(lines)} lines, {len(want['requests'])} requests")
    print("matched:", {k: v for k, v in want["matched"].items() if v})
    missing = [k for k, v in want["matched"].items() if not v]
    if missing:
        print("never seen in this log:", ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
