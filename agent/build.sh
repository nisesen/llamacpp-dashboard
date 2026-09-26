#!/usr/bin/env bash
# Build the agent package into one file, runnable as `python3 <file>`.
#
#   agent/build.sh [output]      default: dist/inferenceinquire-agent.pyz
#
# Stdlib only (python3 -m zipapp), so it builds on the host that runs it.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$here/../dist/inferenceinquire-agent.pyz}"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

cp -R "$here/inferenceinquire_agent" "$stage/"
find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
mkdir -p "$(dirname "$out")"
python3 -m zipapp "$stage" -m "inferenceinquire_agent.__main__:main" \
  -p "/usr/bin/env python3" -c -o "$out"
echo "$out"
