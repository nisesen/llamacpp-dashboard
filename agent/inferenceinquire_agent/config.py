"""Settings, from the environment. Read as `config.NAME` everywhere."""

import os

# This machine only, unless told otherwise: a dashboard elsewhere needs an
# address here, and should be the only one allowed (AGENT_ALLOW).
LISTEN_HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
# Who may connect besides this machine: addresses or networks, separated by
# spaces or commas (e.g. the dashboard's address). Unset, anyone may; the
# bearer token still guards /metrics.json.
ALLOW = os.environ.get("AGENT_ALLOW", "").replace(",", " ").split()
LISTEN_PORT = int(os.environ.get("AGENT_PORT", "9101"))
TOKEN_FILE = os.environ.get("AGENT_TOKEN_FILE", "/etc/inferenceinquire/agent-token")
# Optional pin. Left empty (the default) the agent finds the container itself.
LLM_CTID = os.environ.get("LLM_CTID", "").strip()
# Unit names, and container names, that serve llama.cpp.
UNIT_GLOB = os.environ.get("LLM_UNIT_GLOB", "llama*")
# Where llama.cpp runs: auto, proxmox-lxc, docker, systemd or none. See runtimes/.
RUNTIME = os.environ.get("AGENT_RUNTIME", "auto").strip().lower() or "auto"

# The release, the same as the dashboard's __version__ (a test checks).
AGENT_VERSION = "0.1.0"
# llama.cpp's own default for --cache-ram (host-RAM prompt cache, MiB) when a
# unit does not set it. LLAMA_CACHE_RAM_DEFAULT overrides for other builds.
CACHE_RAM_DEFAULT = int(os.environ.get("LLAMA_CACHE_RAM_DEFAULT", "8192"))
