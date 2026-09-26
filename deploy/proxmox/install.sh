#!/usr/bin/env bash
#
# InferenceInquire on a Proxmox host: the agent on the host, the dashboard in
# a container of its own that already runs Docker. Run as root on the Proxmox
# host, from a checkout of the repository.
#
#   deploy/proxmox/install.sh
#
#   DASH_CTID   the dashboard's container (default 101)
#   AGENT_PORT  default 9101
#   HOST_IP     the host's address as the container reaches it (default: the
#               address on the interface that carries the default route)
#
# What it touches:
#   host         the agent - see deploy/install-agent.sh
#   CT DASH_CTID /opt/inferenceinquire/   the app, its compose file, .env, data/
#                /etc/systemd/system/inferenceinquire.service
#
# It does not modify the inference container or any llama.cpp unit. The
# inference container is found by the agent: the one with a llama* unit.
#
# Settings for this site (a probe fingerprint, PUSH_SKIP_KEYS, NOTIFY_URLS,
# alert tuning) go in site.env at the top of the checkout, as KEY=VALUE lines
# kept out of git, and are added to the dashboard's environment.
#
# Idempotent: re-run it after any change. An install from before v0.1 (the
# llm-host-agent service, /opt/llm-dashboard in the container) is migrated:
# its token and its history database are kept.

set -euo pipefail

DASH_CTID="${DASH_CTID:-101}"
AGENT_PORT="${AGENT_PORT:-9101}"
HOST_IP="${HOST_IP:-$(ip -4 -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP=/opt/inferenceinquire
OLD_APP=/opt/llm-dashboard

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
in_ct() { pct exec "$DASH_CTID" -- bash -c "$1"; }

[[ $EUID -eq 0 ]] || die "run as root on the Proxmox host"
command -v pct >/dev/null || die "pct not found - this must run on the Proxmox host"
pct status "$DASH_CTID" >/dev/null 2>&1 || die "CT $DASH_CTID does not exist"
[[ -n "$HOST_IP" ]] || die "could not work out the host's address - set HOST_IP"

# ---------------------------------------------------------------- 1. agent --
# On every address - the dashboard's container reaches it over the bridge - but
# only for that container, when its address is fixed. A DHCP lease can move.
DASH_IP="$(pct config "$DASH_CTID" | sed -n 's/^net0:.*[,:]ip=\([0-9.]*\)\/.*/\1/p')"
bash "$SRC/deploy/install-agent.sh" --listen 0.0.0.0 --port "$AGENT_PORT" --allow "${DASH_IP}"
if [[ -z "$DASH_IP" ]]; then
  info "CT ${DASH_CTID} has no fixed address: any host may connect (with the token)"
fi
AGENT_TOKEN="$(cat /etc/inferenceinquire/agent-token)"

# --------------------------------------- 2. ask the agent what it found -----
say "Asking the agent what is running"
DISCOVERY=""
for i in $(seq 1 40); do
  DISCOVERY="$(curl -fsS -H "Authorization: Bearer ${AGENT_TOKEN}" \
               "http://127.0.0.1:${AGENT_PORT}/metrics.json" || true)"
  if printf '%s' "$DISCOVERY" | grep -q '"vmid"'; then break; fi
  sleep 1
done
read -r LLM_CTID KEY_FILE ACTIVE_UNIT <<<"$(printf '%s' "$DISCOVERY" | python3 -c '
import json, sys
ct = json.load(sys.stdin).get("guests", {}).get("llm_ct", {}) or {}
units = ct.get("units", {})
active = ct.get("active_unit")
u = units.get(active, {}) if active else (next(iter(units.values()), {}) if units else {})
key = u.get("key_file") or next((v.get("key_file") for v in units.values() if v.get("key_file")), "")
print(ct.get("vmid") or "", key or "", active or "")
')"
[[ -n "$LLM_CTID" ]] || die "no container with a llama* unit was found - is the inference CT running?"
info "inference container : CT ${LLM_CTID}"
info "active unit         : ${ACTIVE_UNIT:-none active}"
LLAMA_KEY=""
if [[ -n "$KEY_FILE" ]]; then
  LLAMA_KEY="$(pct exec "$LLM_CTID" -- cat "$KEY_FILE" 2>/dev/null | tr -d '\r\n')"
fi
if [[ -n "$LLAMA_KEY" ]]; then
  info "api key             : read from ${KEY_FILE} (${#LLAMA_KEY} chars, not printed)"
else
  info "api key             : none - the dashboard will poll unauthenticated"
fi

# ------------------------------------------------------ 3. ship the app -----
say "Shipping the dashboard into CT ${DASH_CTID}"
pct status "$DASH_CTID" | grep -q running || pct start "$DASH_CTID"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$SRC/app" "$STAGE/app"
cp "$SRC/deploy/proxmox/compose.yaml" "$SRC/deploy/proxmox/inferenceinquire.service" "$STAGE/"
find "$STAGE" \( -name __pycache__ -o -name .pytest_cache -o -name '._*' -o -name .DS_Store \) \
  -prune -exec rm -rf {} +
tar -C "$STAGE" -czf "$STAGE.tgz" .
in_ct "mkdir -p $APP"
pct push "$DASH_CTID" "$STAGE.tgz" /tmp/inferenceinquire.tgz
rm -f "$STAGE.tgz"
# Replace the app, never data/ or .env.
in_ct "rm -rf $APP/app && tar -xzf /tmp/inferenceinquire.tgz -C $APP && rm -f /tmp/inferenceinquire.tgz"
info "unpacked to ${APP}"

# ------------------------------------------------------ 4. configuration ----
say "Writing ${APP}/.env"
ENV_FILE="$(mktemp)"
{
  echo "AGENT_URL=http://${HOST_IP}:${AGENT_PORT}"
  echo "AGENT_TOKEN=${AGENT_TOKEN}"
  # No LLAMA_URL: the dashboard follows the endpoint the agent discovers, so a
  # model switch or a moved container needs no change here.
  echo "LLAMA_API_KEY=${LLAMA_KEY}"
  echo "DB_PATH=/data/metrics.db"
  if [[ -f "$SRC/site.env" ]]; then
    echo
    echo "# --- site.env ---"
    grep -v '^[[:space:]]*#' "$SRC/site.env" | grep '=' || true
  fi
} > "$ENV_FILE"
pct push "$DASH_CTID" "$ENV_FILE" "$APP/.env.new"
rm -f "$ENV_FILE"
in_ct "chmod 600 $APP/.env.new && mv $APP/.env.new $APP/.env"
if [[ -f "$SRC/site.env" ]]; then
  info "added site.env: $(grep -v '^[[:space:]]*#' "$SRC/site.env" | grep -o '^[A-Z_]*=' | tr -d = | tr '\n' ' ')"
fi
info "secrets are 0600 and stay inside CT ${DASH_CTID}"

# --------------------------------------------------- 5. build, then swap ----
say "Building the image"
# While the running dashboard keeps serving: the swap below is seconds long.
in_ct "cd $APP && docker compose build --pull -q"

if in_ct "test -d $OLD_APP"; then
  say "Migrating the install from before v0.1 (${OLD_APP})"
  in_ct "systemctl disable --now llm-dashboard.service >/dev/null 2>&1 || true
         cd $OLD_APP && docker compose down --remove-orphans
         rm -f /etc/systemd/system/llm-dashboard.service
         systemctl daemon-reload
         if [ -d $OLD_APP/data ] && [ ! -e $APP/data/metrics.db ]; then
           rm -rf $APP/data && mv $OLD_APP/data $APP/data
         fi
         rm -rf $OLD_APP"
  info "history database moved to ${APP}/data; old container and service removed"
fi

say "Starting it"
in_ct "mkdir -p $APP/data && chown -R 10001:10001 $APP/data
       cd $APP && docker compose up -d --remove-orphans"

# ------------------------------------------------------ 6. boot survival ----
in_ct "install -m 0644 $APP/inferenceinquire.service /etc/systemd/system/inferenceinquire.service
       systemctl daemon-reload
       systemctl enable docker.service >/dev/null
       systemctl enable inferenceinquire.service >/dev/null"
pct set "$DASH_CTID" --onboot 1 >/dev/null
info "CT ${DASH_CTID} onboot=1 · docker.service and inferenceinquire.service enabled"

# ------------------------------------------------------------ 7. verify -----
say "Verifying"
IP="$(pct exec "$DASH_CTID" -- hostname -I | awk '{print $1}')"
for i in $(seq 1 40); do
  if curl -fsS "http://${IP}/healthz" >/dev/null 2>&1; then break; fi
  [[ $i -eq 40 ]] && die "the dashboard did not answer - pct exec ${DASH_CTID} -- docker logs inferenceinquire"
  sleep 1
done
sleep 5  # a couple of polls
curl -fsS "http://${IP}/api/state" | python3 -c '
import json, sys
s = json.load(sys.stdin)["snap"]
m, l = s.get("model", {}), s.get("llama", {})
print("    model      :", m.get("alias"), "|", m.get("file"))
print("    gpus       :", len(s.get("gpus", [])))
print("    llama ok   :", l.get("healthy"), "|", l.get("url"), "|", l.get("latency_ms"), "ms")
print("    agent ok   :", s.get("agent_ok"))
print("    alerts     :", len(s.get("alerts", [])))
'
say "Done - http://${IP}/"
