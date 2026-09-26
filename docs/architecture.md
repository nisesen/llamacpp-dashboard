# Architecture

```
  llama.cpp                                    the machine it runs on
  (a container, a systemd unit                 ┌─────────────────────────────┐
   or a Proxmox LXC)                           │ agent                       │
     │  /health /metrics /props /slots         │  nvidia-smi · /proc · IPMI  │
     │                                         │  Docker API · systemd       │
     │                    ┌────────────────────│  the server's log           │
     ▼                    ▼  /metrics.json     └─────────────────────────────┘
  ┌─────────────────────────────────────┐        (bearer token)
  │ dashboard                            │
  │  poll every 2 s → snapshot → alerts  │───► notifications (Apprise)
  │  SQLite: raw 14 d, rollups 2 y       │
  │  HTTP API and a WebSocket            │───► the page (ES modules, uPlot)
  └─────────────────────────────────────┘
```

Two rules shape the code:

- **Discover, don't declare.** Where llama.cpp runs, its units and settings,
  each card's limits, which network interface matters, how fast a model should
  be: all of it is read or learned at runtime. Adding a card or a model needs no
  setting.
- **Read only, and cheap.** Every command the agent runs is a query. It costs
  about 0.03 of a CPU core on a busy server: it keeps one `nvidia-smi` running
  in loop mode rather than starting one per second, and it reads Proxmox's own
  files instead of running its command-line tools. A page update is about 5 KB.

## The agent

`agent/inferenceinquire_agent/`, standard library only, so it runs on the
host's own Python (3.11+) as one file built by `agent/build.sh`, or in its
image (`agent/Dockerfile`).

| Module | What it does |
|---|---|
| `collectors/nvidia.py` | the GPU stream (`nvidia-smi` in loop mode), per-card limits, processes, PCIe, Xid |
| `collectors/host.py` | CPU, memory, network, disks, uptime from `/proc` |
| `collectors/ipmi.py` | BMC sensors and the event log |
| `collectors/gguf.py` | the loaded model's shape, from its GGUF header |
| `runtimes/` | where llama.cpp runs: `proxmox_lxc`, `docker`, `systemd`, `none` |
| `journal/parser.py` | llama.cpp's log lines into requests, live slots, the load state and counters |
| `journal/follower.py` | follows the serving unit's log, and moves to another on a model switch |
| `cache.py`, `payload.py` | every collector refreshed off-thread on its own schedule, and the JSON document |
| `server.py` | `/metrics.json` behind the token, `/healthz`, the address allowlist |
| `selftest.py` | the checks the installers run before replacing a working agent |

A **runtime** is a module with four functions:

- `available()`: can it work on this machine at all;
- `collect()`: what runs here, as `{"guests": [...], "llm_ct": {...}}`, where
  `llm_ct` names the target (its `runtime`, a `label`), its llama.cpp units
  with their settings, the `active_unit` and the `endpoint` it answers on (the
  name `llm_ct` is older than the other runtimes);
- `follow(target, unit)`: a stream of that unit's log, shaped like
  `journalctl -o short-unix` lines, so one parser reads every runtime. The
  stream can be ended from another thread, and ends only what this agent
  started;
- `read_head(target, unit, path, n)`: the first bytes of a file there.

`runtimes/__init__.py` picks one (`AGENT_RUNTIME`).

## The dashboard

`app/inferenceinquire/`, FastAPI and httpx, run by `python -m inferenceinquire`.

| Module | What it does |
|---|---|
| `config.py` | every setting, read once from the environment |
| `sources.py` | reads the agent and llama.cpp; a source that fails is reported, never raised |
| `derive.py` | pure functions from readings to figures: rates from counters, per-card rows, host usage |
| `workload.py` | requests: probe or workload, and what the page shows of each |
| `monitor.py` | the poll loop: read, derive, judge, persist, broadcast |
| `rules.py` | the alert rules, each a generator over the snapshot |
| `alerts.py` | hold times, model-switch transitions and what is worth pushing |
| `notify.py` | push notifications |
| `store.py`, `schema.py`, `usage.py` | SQLite: samples, requests, events, rollups, retention, usage |
| `snapshot.py` | what each WebSocket update carries: only the sections that changed |
| `api.py` | the page, the HTTP API, the WebSocket and `/metrics` |
| `demo.py` | demo mode: a recorded session replayed as if it were live |

The poll loop reads llama.cpp's cumulative counters and works out rates as the
difference between two polls; see [metrics.md](metrics.md) for why the
throughput figures are per request. Every fifth poll is folded into one stored
row, with means, sums and peaks.

## The page

`app/static/`: `index.html`, one stylesheet and ES modules under `js/`, with no
build step. `main.js` starts it; `data.js` holds the WebSocket and fetches
history; `state.js` holds what the page knows; `sections/` draw the cards;
`charts/` wraps [uPlot](https://github.com/leeoniya/uPlot), vendored in
`static/vendor/uplot/` so the page needs no network beyond the dashboard.

## Deployment

- `compose.yaml`: the dashboard and the agent on one Docker host.
- `deploy/install-agent.sh`: the agent as a systemd service.
- `deploy/proxmox/`: the Proxmox installer, its compose file and boot unit.
- `.github/workflows/ci.yml`: lint, tests on Python 3.11 and 3.13, the page in
  a browser, both images run together, and both built for amd64 and arm64,
  published to GHCR for a version tag.
