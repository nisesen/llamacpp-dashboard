# Configuration

Every setting is an environment variable, and every one has a working default.

- With `compose.yaml`, put them in `.env` next to it.
- With `docker run`, pass them with `-e`.
- On Proxmox, put the dashboard's in `site.env` at the top of the checkout.
- The agent service reads `/etc/inferenceinquire/agent.env`.

## The dashboard

### Where things are

| Setting | Default | Meaning |
|---|---|---|
| `LLAMA_URL` | what the agent finds | Where llama.cpp answers, e.g. `http://127.0.0.1:8080`. Set, it is used as given. Unset, the dashboard uses the endpoint the agent discovers, which follows a model switch or a moved container. |
| `LLAMA_API_KEY` | none | llama-server's `--api-key`, needed for `/metrics` and `/slots`. |
| `AGENT_URL` | unset | The agent, e.g. `http://127.0.0.1:9101`. Unset, there is no agent: the page shows llama.cpp's own endpoints and hides the rest. |
| `AGENT_TOKEN` | none | The agent's bearer token. |
| `AGENT_TOKEN_FILE` | unset | Read the token from this file instead, creating it (mode 0600) if it does not exist. `compose.yaml` uses this to share a token through a volume. |
| `PORT` | 8000 | The port the dashboard listens on. |
| `LISTEN_ADDRESS` | 0.0.0.0 | The address it listens on. |
| `DB_PATH` | `/data/metrics.db` | The history database. |

### History

| Setting | Default | Meaning |
|---|---|---|
| `POLL_INTERVAL` | 2 | Seconds between polls. |
| `PERSIST_EVERY` | 5 | Polls folded into each stored row. |
| `RETAIN_DAYS` | 14 | Days of raw rows and per-request detail. |
| `ROLLUP_5M_DAYS` | 90 | Days of 5-minute rollups, which serve the 30d and 90d ranges. |
| `ROLLUP_1H_DAYS` | 730 | Days of hourly rollups, which serve the 1y range. |

### Alerts

See [alerts.md](alerts.md) for what each rule does.

| Setting | Default | Meaning |
|---|---|---|
| `POWER_WARN_RATIO` / `POWER_SERIOUS_RATIO` | 1.15 / 1.35 | Power alert levels, as multiples of each card's own cap. |
| `POWER_RUN_TO_ALERT` | 6 | Polls in a row over the cap before it alerts. |
| `SLOW_FRACTION` | 0.25 | Decode below this share of the model's learned median is slow. |
| `SLOW_RUN_TO_ALERT` | 3 | Slow requests in a row before it alerts. |
| `COLD_SLOW_TPS` | 5 | The slow-decode floor before a model has a baseline. |
| `TRANSITION_MAX` | 300 | Seconds a restart or model switch may take before its alerts show. |
| `PUSH_SKIP_KEYS` | none | Alert keys not to push, comma-separated, e.g. `llama,llama_health,units` when another monitor already watches reachability. |

### Notifications

| Setting | Default | Meaning |
|---|---|---|
| `NOTIFY_URLS` | unset | [Apprise](https://github.com/caronc/apprise/wiki) URLs, separated by spaces. |
| `NOTIFY_COMMAND` | unset | A command to run per message: the text on stdin, the title in `$NOTIFY_TITLE`. |
| `NOTIFY_TITLE` | InferenceInquire | The messages' title, handy with several servers. |
| `NOTIFY_COOLDOWN` | 1800 | Seconds before a non-critical alert that cleared may be announced again. |
| `DASHBOARD_URL` | unset | The page's address, added to each message as a link. |

### Requests and the probe

| Setting | Default | Meaning |
|---|---|---|
| `PROBE_GEN_TOKENS` | unset | A health-check probe's fingerprint: exactly this many tokens generated. See [features.md](features.md#probe-requests). |
| `PROBE_MAX_TOKENS` | 256 | ...and fewer than this many in the slot. |
| `COLD_PREFILL_TOKENS` | 8192 | Computed prompt tokens that make a request a cold prefill. |
| `SLOTS_POLL` | `auto` | When to read `/slots`: `auto` (unless the server sleeps when idle), `busy` (only while requests run) or `always`. See [metrics.md](metrics.md#llamacpps-sleep-mode). |

### Energy

| Setting | Default | Meaning |
|---|---|---|
| `ENERGY_PRICE_PER_KWH` | unset | Electricity price, to show cost per day and per million tokens. |
| `ENERGY_CURRENCY` | `$` | Its currency symbol. |

### Demo mode

| Setting | Default | Meaning |
|---|---|---|
| `DEMO` | unset | `1` replays a recorded session instead of polling anything. |
| `DEMO_OFFSET` | 0 | Seconds into the recording to start; 1200 is where a mixture-of-experts model is loaded. |
| `DEMO_AGENT` | 1 | `0` replays it as a dashboard without the agent would see it. |

## The agent

| Setting | Default | Meaning |
|---|---|---|
| `AGENT_HOST` | 127.0.0.1 | The address it listens on: this machine only. Give an address here when the dashboard runs elsewhere. |
| `AGENT_PORT` | 9101 | Its port. |
| `AGENT_TOKEN_FILE` | `/etc/inferenceinquire/agent-token` | Its bearer token, read on every request. |
| `AGENT_ALLOW` | unset | Addresses or networks that may connect besides this machine, e.g. `192.0.2.20` or `10.0.0.0/24`. Unset, any may (the token still guards the data). |
| `AGENT_RUNTIME` | `auto` | Where llama.cpp runs: `proxmox-lxc`, `docker`, `systemd` or `none`. `auto` uses Proxmox on a Proxmox host, and otherwise whichever of Docker and systemd has a llama.cpp server running. |
| `LLM_UNIT_GLOB` | `llama*` | systemd units, and container names, that serve llama.cpp. |
| `LLM_CTID` | unset | Proxmox: the container to watch, instead of finding it. |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker: the Engine API, as `unix://path` or `tcp://host:port` (for a socket proxy; see [security.md](security.md)). |
| `LLAMA_CACHE_RAM_DEFAULT` | 8192 | The `--cache-ram` a server without that flag is reported with, in MiB. |

## compose.yaml

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | 8000 | The dashboard's port. |
| `AGENT_PORT` | 9101 | The agent's port, on 127.0.0.1. |
| `TAG` | 0.1 | The image version. |
