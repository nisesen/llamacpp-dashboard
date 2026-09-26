# Contributing

Thank you for helping. Bug reports, log samples from llama.cpp builds we have
not seen, documentation fixes and pull requests are all welcome. For anything
large, please open an issue first so we can agree on the approach.

## Setting up

You need Python 3.11 or newer. No GPU, llama.cpp or agent is needed: demo mode
replays a recorded session.

```bash
git clone https://github.com/nisesen/inferenceinquire.git
cd inferenceinquire
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pre-commit install          # ruff, formatting, shellcheck and a secret scan on each commit

cd app && DEMO=1 python -m inferenceinquire      # http://localhost:8000
```

- `DEMO_OFFSET=1200` starts where a mixture-of-experts model is loaded, for
  the Anatomy tab.
- `DEMO_AGENT=0` shows the page as a dashboard without the agent sees it.
- Against a real server instead: `LLAMA_URL=http://...:8080` and, with an
  agent, `AGENT_URL` and `AGENT_TOKEN`.

The page is plain ES modules with no build step: edit a file in `app/static/`
and reload.

## Tests

```bash
pytest                                   # the dashboard's and the agent's unit tests
pytest e2e                               # the page in a browser, on demo mode
python tools/replay_check.py main        # a change that should not alter behaviour, proven
```

- `pytest e2e` uses Playwright: run `playwright install chromium` once, or set
  `PLAYWRIGHT_CHANNEL=chrome` to use an installed Chrome. It loads every tab at
  desktop and phone width and fails on a console error or a page wider than
  the screen.
- `tools/replay_check.py <git-ref>` replays the demo recording through the
  poll loop of that version and of your working tree, with a fake clock, and
  compares every WebSocket update, API answer, log line and database row. For
  a refactor it must print `IDENTICAL`; otherwise it shows the first
  differences, which should be exactly the ones you meant.
- CI runs all of these, builds both images and runs them together.

## Making a change

- Keep the agent to the standard library: it runs on the host's own Python.
- Match the code around you: short functions, comments that say why, names
  from the domain. `ruff` formats and lints (line length 100).
- A change the user can see needs a test, and a line in `CHANGELOG.md` under
  "Unreleased".
- A pull request should say what changed, why, and how you checked it.

## Common contributions

### Logs from a new llama.cpp build

The per-request panels come from llama.cpp's log, and its wording can change
between builds. If yours is not parsed (the dashboard warns "log format
changed?"), a fixture is the most useful thing you can send:

```bash
journalctl -u llama-server -o short-unix --no-pager > log.txt    # systemd
docker logs --timestamps llama > log.txt 2>&1                    # Docker
python tools/log_fixture.py log.txt llama-server-b12345
pytest agent/tests/test_journal.py
```

It keeps a few complete requests and one line of every other pattern, and
records what the parser makes of them. Read the `.log` before you commit it,
and replace anything private, such as paths. Then fix the pattern in
`agent/inferenceinquire_agent/journal/patterns.py` until the test passes on
every fixture, old ones included.

### A runtime: where llama.cpp runs

The agent reaches llama.cpp through a runtime module in
`agent/inferenceinquire_agent/runtimes/`: `available()`, `collect()`,
`follow(target, unit)` and `read_head(target, unit, path, n)`.
[docs/architecture.md](docs/architecture.md#the-agent) describes what each
returns; `docker.py` and `systemd.py` are short examples. Register it in
`runtimes/__init__.py`, and test it against fake data as
`agent/tests/test_runtimes.py` does.

### A GPU vendor

GPU readings come from `agent/inferenceinquire_agent/collectors/nvidia.py`,
as rows with the fields the dashboard reads (`derive.gpu_rows` shows which).
Another vendor is a collector that produces the same rows from its own tools
or sysfs, chosen by what the machine has.

### Screenshots and demo data

- `python tools/screenshots.py` retakes the documentation's screenshots from
  demo mode.
- `python tools/demo_data.py record ...` records a session from your own
  server, and `scrub` removes host names, addresses, GPU serials and guest
  names from it. Run it without arguments for the details.

## Reporting a security problem

Please do not open a public issue; see [SECURITY.md](SECURITY.md).
