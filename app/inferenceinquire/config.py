"""Settings: everything site-specific or tunable, in one place.

Read once from the environment at import. Other modules read them as
`config.NAME`, never as copies, so a test can override one in one place.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, "") or default)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, "") or default)


# Where in the recording demo mode starts, in seconds. The recording holds a
# switch to a mixture-of-experts model from about 1100 s to 1600 s.
DEMO_OFFSET = _env_float("DEMO_OFFSET", 0.0)

# Demo mode replays a recorded session instead of polling a real machine, over
# a fresh store seeded with a recorded week. See demo.py.
DEMO = os.environ.get("DEMO", "").strip().lower() not in ("", "0", "false", "no")

# The host agent, for GPU, host and per-request telemetry. Unset, the dashboard
# runs on llama.cpp's own endpoints alone and hides what needs the agent. The
# demo's recording carries an agent; DEMO_AGENT=0 replays it without one.
AGENT_URL = os.environ.get("AGENT_URL", "").rstrip("/") or (
    "http://agent.demo:9101" if DEMO and os.environ.get("DEMO_AGENT", "1") != "0" else ""
)
AGENT_ENABLED = bool(AGENT_URL)
# The agent's bearer token: AGENT_TOKEN, or read from AGENT_TOKEN_FILE, which
# is created if it does not exist yet (the compose file shares it with the
# agent through a volume). See sources.agent_token().
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
AGENT_TOKEN_FILE = os.environ.get("AGENT_TOKEN_FILE", "")
# Where llama.cpp answers. Set, it is used as given. Unset, the dashboard
# follows the endpoint the agent discovers, which moves with a model switch or
# a moved container.
LLAMA_URL = os.environ.get("LLAMA_URL", "").rstrip("/") or (
    "http://llama.demo:8080" if DEMO and not AGENT_ENABLED else ""  # nobody to discover it
)
LLAMA_KEY = os.environ.get("LLAMA_API_KEY", "")
POLL_INTERVAL = _env_float("POLL_INTERVAL", 2.0)
PERSIST_EVERY = _env_int("PERSIST_EVERY", 5)
RETAIN_DAYS = _env_float("RETAIN_DAYS", 14)
# Long-term history. Raw rows are kept RETAIN_DAYS; beyond that, 5-minute and
# hourly rollups keep the shape - means, sums and peaks - for months and
# years, which is what slow wear (HBM temperature creep, remapped rows, a
# decode baseline drifting after an engine upgrade) needs to show.
ROLLUP_5M_DAYS = _env_float("ROLLUP_5M_DAYS", 90)
ROLLUP_1H_DAYS = _env_float("ROLLUP_1H_DAYS", 730)
ROLLUPS = {"5m": 300, "1h": 3600}
DB_PATH = os.environ.get("DB_PATH") or (
    str(Path(tempfile.mkdtemp(prefix="inferenceinquire-demo-")) / "metrics.db")
    if DEMO
    else "/data/metrics.db"
)
LIVE_MINUTES = 90
LIVE_POINTS = int(LIVE_MINUTES * 60 / POLL_INTERVAL)

# --- alert tuning: ratios of what the hardware reports, overridable --------
# Power headroom is expressed as a multiple of whatever cap the card reports,
# so re-capping a GPU moves these with it.
POWER_WARN_RATIO = _env_float("POWER_WARN_RATIO", 1.15)
POWER_SERIOUS_RATIO = _env_float("POWER_SERIOUS_RATIO", 1.35)
# How many consecutive polls over the cap before it is worth telling anyone.
#
# Prefill is usually the most power-hungry phase and briefly exceeds the cap
# by design: cards enforce their limit as a rolling average, not
# instantaneously, and a single 2 s poll can catch the overshoot. Six polls
# (~12 s) stays quiet for those bursts while still catching a card that *sits*
# over its cap, which is the case that points at power delivery. Raising the
# ratio instead would blind exactly that case.
POWER_RUN_TO_ALERT = _env_int("POWER_RUN_TO_ALERT", 6)
# kv: {"<gpu idx>": [consecutive polls over the warn ratio, peak watts]}
POWER_RUN_KEY = "power_run"
# A decode rate this far below the model's own learned median means something
# is wrong - most likely llama.cpp silently running on the CPU.
SLOW_FRACTION = _env_float("SLOW_FRACTION", 0.25)
# Only used before a model has a baseline: no GPU path on any plausible model
# is this slow, while a CPU fallback of a large model usually is.
COLD_SLOW_TPS = _env_float("COLD_SLOW_TPS", 5.0)
MIN_BASELINE_SAMPLES = 12
BASELINE_WINDOW = 300
# How many consecutive slow OBSERVATIONS before slow_decode is believed.
#
# One slow observation nearly always means the request shared the GPU, not
# that the GPU is broken. llama.cpp charges each slot the WALL time it spent
# generating, so a request that interleaved with another slot's prefill
# reports its own rate as a small fraction of the single-stream baseline while
# aggregate throughput across the slots is entirely normal. A genuine CPU
# fallback makes EVERY observation slow, so it clears this bar within a few
# requests.
SLOW_RUN_TO_ALERT = _env_int("SLOW_RUN_TO_ALERT", 3)
# kv: [run, last_tps, last_baseline]
SLOW_RUN_KEY = "slow_decode_run"

# Synthetic "probe" requests - an external health check that sends the same
# small request on a timer - are real traffic to llama.cpp but not workload.
# Left in, a frequent probe can dominate every per-request figure. They are
# recognised by their journal fingerprint: exactly PROBE_GEN_TOKENS generated,
# fewer than PROBE_MAX_TOKENS in the slot. Unset (the default) disables it.
# The demo recording carries a 48-token health probe, so demo mode shows it apart.
PROBE_GEN_TOKENS = _env_int("PROBE_GEN_TOKENS", 48 if DEMO else 0) or None
PROBE_MAX_TOKENS = _env_int("PROBE_MAX_TOKENS", 256)
# A request that had to compute at least this much of its prompt paid for a
# cold prefill: seconds of waiting at typical prefill rates, minutes at depth.
COLD_PREFILL_TOKENS = _env_int("COLD_PREFILL_TOKENS", 8192)
# Prefill rates from a handful of computed tokens are overhead, not
# throughput: a 4-token tail after a cache hit reads "19 tok/s", and a few
# hundred tokens still read well under half the real rate.
MIN_PREFILL_TOKENS = 1024

# Electricity price, for the usage card's cost figures. Unset = no cost shown.
ENERGY_PRICE_PER_KWH = _env_float("ENERGY_PRICE_PER_KWH", 0.0) or None
ENERGY_CURRENCY = os.environ.get("ENERGY_CURRENCY", "$")

# How long a condition must hold before it is an alert. Presence checks flap
# for reasons that are not outages: a restart gap, one failed `pct exec`
# (which the agent then serves for a full 10 s guest-cache cycle). A model
# switch typically leaves the port closed for several seconds before the new
# process answers 503 "Loading model" and the transition takes over.
HOLD_SECONDS = {
    "llama": 20,
    "llama_health": 20,
    "units": 20,
    "units_both": 20,
    "agent": 10,
    "ct": 25,
    "ct_missing": 25,
    "gpu_count": 4,
}
# A model switch or restart is a state, not an outage. While one is visibly in
# progress (a unit activating, /health answering "Loading model", the journal
# mid-load) the reachability alerts are held back - up to a limit, after which
# a load that never finishes alerts like anything else.
TRANSITION_KEYS = {"llama", "llama_health", "units"}
TRANSITION_MAX = _env_float("TRANSITION_MAX", 300.0)
TRANSITION_TAIL = 20.0
# A new BMC record stays on the page this long after it is first seen.
SEL_ALERT_HOLD = 86400.0
SEL_SERIOUS = (
    "Uncorrectable",
    "Non-recoverable",
    "Critical going",
    "Power Supply",
    "Power Unit",
    "Failure",
    "AC lost",
    "Voltage",
)
# What /api/alerts marks as push-worthy (`notify`). Serious and critical
# always; below that, the warnings that tend to precede hardware trouble:
# sustained power draw, temperature, a crash-restart, a new BMC record.
# PUSH_SKIP_KEYS lists alert keys another monitor already reports - e.g. an
# external watchdog that checks reachability from its own side - so nothing
# arrives twice.
PUSH_SKIP_KEYS = {k.strip() for k in os.environ.get("PUSH_SKIP_KEYS", "").split(",") if k.strip()}
NOTIFY_WARNING_PREFIXES = ("pwr:", "hbm:", "gtemp:", "restart:", "sel")
# Where push-worthy alerts go (notify.py): Apprise URLs, space or comma
# separated, and/or a command that gets the message on stdin. Unset, nothing
# is sent, and /api/alerts is there for an external forwarder.
NOTIFY_URLS = os.environ.get("NOTIFY_URLS", "").replace(",", " ").split()
NOTIFY_COMMAND = os.environ.get("NOTIFY_COMMAND", "").strip()
NOTIFY_TITLE = os.environ.get("NOTIFY_TITLE", "").strip() or "InferenceInquire"
# A condition that clears and comes back is not news every time. Criticals
# always are; anything else is re-announced at most this often.
NOTIFY_COOLDOWN = _env_float("NOTIFY_COOLDOWN", 1800.0)
# The page's address, for a link at the end of each message.
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")

# llama.cpp's log format is not an API. When /metrics shows this many
# requests finish in a row and the journal yields timings for none of them,
# the agent's patterns no longer match this build's log.
PARSE_MISSES_TO_ALERT = 3
# kv: {"tok": tokens_predicted_total, "ev": timing lines matched, "misses": n}
PARSE_WATCH_KEY = "parse_watch"


# llama.cpp's --sleep-idle-seconds unloads an idle model and frees its memory.
# Of the endpoints polled here, /health and /props bypass the sleep machinery,
# and /metrics does too from build b10519 (upstream #27376; before it, a scrape
# counted as activity). /slots does not: it is queued like real work, so it
# resets the idle timer - polled every 2 s the model never sleeps - and it
# WAKES a sleeping model. When sleep is in play, /slots is therefore read only
# while requests are running, which keep the model awake anyway.
#   auto    read /slots unless the serving unit sets --sleep-idle-seconds
#   busy    only while requests are running (for setups without the agent)
#   always  every poll
# In every mode a sleeping model is left asleep.
SLOTS_POLL = os.environ.get("SLOTS_POLL", "auto").strip().lower()
