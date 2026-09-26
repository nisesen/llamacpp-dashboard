# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-09-26

The first public release.

### Added

- A dashboard for llama.cpp servers: per-request decode and prefill speed,
  latency, context depth, speculative-decoding acceptance, slots, the KV cache
  and the loaded model's identity and shape.
- GPU telemetry for NVIDIA cards against each card's own limits, host CPU,
  memory, disks and network, and BMC sensors and event log where there is one.
- Alerts that hold back during a restart or model switch, and a learned
  per-model speed baseline that catches a silent fallback to the CPU.
- Push notifications through Apprise URLs (`NOTIFY_URLS`) or a command.
- History for up to two years in SQLite, with 5-minute and hourly rollups.
- An agent that finds llama.cpp in a Docker container, a systemd unit or a
  Proxmox LXC container, and follows its log for per-request timings.
- A dashboard-only mode against any llama-server, with no agent.
- `compose.yaml` for one Docker host; `deploy/install-agent.sh` for the agent
  as a systemd service; `deploy/proxmox/install.sh` for Proxmox.
- Container images for amd64 and arm64 on GHCR.
- Demo mode (`DEMO=1`), which replays a recorded session with no GPU needed.
- `/metrics` in Prometheus format.

[Unreleased]: https://github.com/nisesen/inferenceinquire/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nisesen/inferenceinquire/releases/tag/v0.1.0
