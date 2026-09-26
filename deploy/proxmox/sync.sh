#!/usr/bin/env bash
# Push this working tree to a Proxmox host's checkout and run the installer
# there - for deploying from another machine.
#
#   deploy/proxmox/sync.sh <ssh-host> <checkout-dir-on-the-host>
#
# rsync, not "rm -rf && untar": the host copy is a git repository, and
# deleting the directory would take .git with it. --delete keeps it clean;
# excluded paths are protected from deletion, which is what keeps the host's
# site.env (settings for that site, not in git) alive.
set -euo pipefail
HOST="${1:?usage: deploy/proxmox/sync.sh <ssh-host> <checkout-dir>}"
DEST="${2:?usage: deploy/proxmox/sync.sh <ssh-host> <checkout-dir>}"
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

# --no-owner/--no-group: rsync running as root on the receiver would otherwise
# stamp the sender's uid on every file, and git then refuses the repo as
# "dubious ownership".
rsync -a --no-owner --no-group --delete \
  --exclude='.git' --exclude='data' --exclude='dist' --exclude='__pycache__' --exclude='site.env' \
  --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude='.DS_Store' --exclude='._*' \
  ./ "${HOST}:${DEST}/"

# shellcheck disable=SC2029  # DEST is this side's argument, expanded here on purpose
ssh "$HOST" "cd '${DEST}' && bash deploy/proxmox/install.sh"
