#!/usr/bin/env bash
#
# Install or update the InferenceInquire agent on this machine, as a systemd
# service. Run as root, from a checkout of the repository.
#
#   deploy/install-agent.sh [--listen ADDRESS] [--port PORT] [--allow ADDRESSES]
#
#   --listen  where the agent accepts connections. Default 127.0.0.1: only this
#             machine, which is right when the dashboard runs here too (for
#             example with `docker compose up -d dashboard`). When it runs on
#             another machine, give the address it reaches this one at.
#   --port    default 9101.
#   --allow   who else may connect: the dashboard's address, or networks
#             (comma-separated). Anyone else is refused before any HTTP.
#             Without --allow, an allowlist set earlier is kept.
#
# What it touches:
#   /usr/local/bin/inferenceinquire-agent            the agent (one file, stdlib)
#   /etc/systemd/system/inferenceinquire-agent.service
#   /etc/inferenceinquire/agent-token                generated once, 0600
#   /etc/inferenceinquire/agent.env                  AGENT_HOST, AGENT_PORT, AGENT_ALLOW;
#                                                    other settings you add are kept
#
# Idempotent. The new agent is built and self-tested before anything is
# replaced, and an existing token is kept. An agent installed under its old
# name (llm-host-agent, before v0.1) is replaced, with its token.

set -euo pipefail

LISTEN=127.0.0.1
PORT=9101
ALLOW=""
SET_ALLOW=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --listen) LISTEN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --allow) ALLOW="$2"; SET_ALLOW=1; shift 2 ;;
    -h|--help) sed -n '3,26p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ETC=/etc/inferenceinquire
BIN=/usr/local/bin/inferenceinquire-agent
UNIT=inferenceinquire-agent

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"
command -v systemctl >/dev/null || die "systemctl not found - this installs a systemd service"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
  || die "the agent needs python3 3.11 or newer"

say "Installing the InferenceInquire agent on $(hostname)"

# ---------------------------------------------------------------- token -----
install -d -m 0750 "$ETC"
if [[ ! -s "$ETC/agent-token" && -s /etc/llm-host-agent/token ]]; then
  install -m 0600 /etc/llm-host-agent/token "$ETC/agent-token"
  info "kept the token of the agent installed as llm-host-agent"
elif [[ ! -s "$ETC/agent-token" ]]; then
  (umask 077; python3 -c 'import secrets; print(secrets.token_hex(24))' > "$ETC/agent-token")
  info "generated a new token"
else
  info "keeping the existing token"
fi
chmod 0600 "$ETC/agent-token"

# ------------------------------------------------------------- settings -----
# AGENT_HOST and AGENT_PORT are this script's, and AGENT_ALLOW when --allow is
# given; any other line is yours.
touch "$ETC/agent.env"
chmod 0640 "$ETC/agent.env"
sed -i '/^AGENT_HOST=/d; /^AGENT_PORT=/d' "$ETC/agent.env"
printf 'AGENT_HOST=%s\nAGENT_PORT=%s\n' "$LISTEN" "$PORT" >> "$ETC/agent.env"
if [[ $SET_ALLOW -eq 1 ]]; then
  sed -i '/^AGENT_ALLOW=/d' "$ETC/agent.env"
  if [[ -n "$ALLOW" ]]; then printf 'AGENT_ALLOW=%s\n' "$ALLOW" >> "$ETC/agent.env"; fi
fi

# ------------------------------------------------------ build and check -----
# Built and checked before it replaces anything: a failed build or self-test
# leaves the running agent, and the file it was started from, untouched. The
# journal patterns are the fragile part - llama.cpp's log format is not an API.
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
bash "$SRC/agent/build.sh" "$BUILD/agent.pyz" >/dev/null
if ! python3 "$BUILD/agent.pyz" --self-test >"$BUILD/selftest.log" 2>&1; then
  cat "$BUILD/selftest.log"
  die "agent self-test failed - nothing was changed"
fi
info "self-test passed ($(grep -c '^ok' "$BUILD/selftest.log") checks)"

# -------------------------------------------- the agent under its old name --
if [[ -e /etc/systemd/system/llm-host-agent.service ]]; then
  systemctl disable --now llm-host-agent.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/llm-host-agent.service
  info "removed llm-host-agent.service (the agent's name before v0.1)"
fi
rm -f /usr/local/sbin/llm-host-agent.pyz /usr/local/sbin/llm-host-agent.py
if [[ -s "$ETC/agent-token" ]]; then rm -rf /etc/llm-host-agent; fi

# -------------------------------------------------------------- install -----
install -m 0755 "$BUILD/agent.pyz" "$BIN"
install -m 0644 "$SRC/agent/inferenceinquire-agent.service" "/etc/systemd/system/$UNIT.service"
systemctl daemon-reload
systemctl enable "$UNIT.service" >/dev/null
systemctl restart "$UNIT.service"

HERE="$LISTEN"
if [[ "$HERE" == 0.0.0.0 || "$HERE" == "::" ]]; then HERE=127.0.0.1; fi
for i in $(seq 1 20); do
  curl -fsS "http://$HERE:$PORT/healthz" >/dev/null 2>&1 && break
  [[ $i -eq 20 ]] && die "the agent did not come up - journalctl -u $UNIT"
  sleep 0.5
done
# Discovery lands a few seconds after the socket opens.
sleep 3
curl -fsS -H "Authorization: Bearer $(cat "$ETC/agent-token")" "http://$HERE:$PORT/metrics.json" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
ct = d.get("guests", {}).get("llm_ct") or {}
print("    GPUs       :", len(d.get("gpu", {}).get("gpus", [])))
print("    llama.cpp  :", ct.get("runtime"), "|", ct.get("label") or "not found yet", "|",
      ct.get("active_unit") or "-", "|", ct.get("endpoint") or "-")
' || info "(the agent answers, but its report could not be read yet)"

ALLOWED="$(sed -n 's/^AGENT_ALLOW=//p' "$ETC/agent.env")"
say "Agent running on $LISTEN:$PORT${ALLOWED:+, for this machine and $ALLOWED only}"
info "token: $ETC/agent-token - the dashboard needs it as AGENT_TOKEN"
info "logs:  journalctl -u $UNIT"
