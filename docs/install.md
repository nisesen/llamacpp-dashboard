# Installing

InferenceInquire is two programs:

- the **dashboard**: a web page and the service behind it, which polls
  llama.cpp and the agent every 2 seconds and keeps the history;
- the **agent** (optional): a small read-only program on the machine that runs
  llama.cpp. It reads the GPUs, the host, and llama.cpp's own log, which holds
  the per-request timings.

Pick the setup that matches yours.

- [Before you start](#before-you-start)
- [Docker, dashboard and agent together](#docker-dashboard-and-agent-together)
- [Just the dashboard](#just-the-dashboard)
- [The agent as a systemd service](#the-agent-as-a-systemd-service)
- [Proxmox, with llama.cpp in an LXC container](#proxmox-with-llamacpp-in-an-lxc-container)
- [Running from source](#running-from-source)
- [Upgrading](#upgrading)
- [Removing it](#removing-it)

## Before you start

- **llama-server needs `--metrics`.** Without it `/metrics` does not exist and
  the throughput charts stay empty.
- If llama-server runs with `--api-key`, give the dashboard the same key as
  `LLAMA_API_KEY`: `/metrics` and `/slots` need it.
- The agent needs Linux, and Python 3.11 or newer when it runs outside Docker.
  GPU telemetry needs NVIDIA's `nvidia-smi`; other GPU vendors are not
  supported yet.
- The per-request panels come from llama.cpp's log, whose wording changes
  between builds. Builds from b10802 to b11176 are tested. If a newer build
  breaks them, the dashboard says so with a "log format changed?" warning;
  please open an issue with a log sample.

## Docker, dashboard and agent together

For a Linux machine with Docker Compose 2.24 or newer, where llama.cpp runs in
a container or as a systemd unit.

```bash
git clone https://github.com/nisesen/inferenceinquire.git
cd inferenceinquire
cp .env.example .env    # optional: every setting has a default
docker compose up -d
```

Open http://your-machine:8000.

- **GPUs.** The agent reads NVIDIA GPUs through the
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
  Without an NVIDIA GPU, delete the `gpus: all` line in `compose.yaml`;
  everything else still works.
- **Finding llama.cpp.** The agent looks for a container whose name starts
  with `llama`, whose image is llama.cpp's, or whose command is
  `llama-server`, and reads its settings from its command line and
  `LLAMA_ARG_*` variables. It follows that container's log, and a model
  switch between containers is followed too.
- **llama.cpp under systemd** on the same machine: the agent in a container
  cannot ask the host's systemd. Run the agent as a
  [systemd service](#the-agent-as-a-systemd-service) instead, and start only
  the dashboard: `docker compose up -d dashboard`, with `AGENT_TOKEN` in
  `.env` (see that section).
- **Networking.** Both containers use the host's network. The dashboard
  reaches a llama-server that listens on 127.0.0.1 (llama-server's default),
  and the agent listens on 127.0.0.1 only. The dashboard's port is `PORT`,
  default 8000.
- **What the agent is allowed.** The host's process list (to see which
  llama-server is running and read its model file's header), the GPUs, the
  kernel log (`CAP_SYSLOG`, for GPU faults) and the Docker socket (read with
  GET requests only). Both containers run read-only. See
  [security.md](security.md).
- **A BMC.** On a board with IPMI, uncomment the `devices:` line to pass
  `/dev/ipmi0` to the agent.

If the images cannot be pulled, `docker compose up -d` builds them from the
checkout.

## Just the dashboard

Next to any llama-server, with nothing else installed. It shows what llama.cpp's
own endpoints report: throughput, requests, slots, the KV cache and the model.

On Linux, share the host's network so the dashboard can reach a llama-server
that listens on 127.0.0.1:

```bash
docker run -d --name inferenceinquire --network host --restart unless-stopped \
  -e LLAMA_URL=http://127.0.0.1:8080 -v inferenceinquire:/data \
  ghcr.io/nisesen/inferenceinquire:0.1
```

With Docker Desktop on macOS or Windows, `host.docker.internal` is the host:

```bash
docker run -d --name inferenceinquire -p 8000:8000 --restart unless-stopped \
  -e LLAMA_URL=http://host.docker.internal:8080 -v inferenceinquire:/data \
  ghcr.io/nisesen/inferenceinquire:0.1
```

Add `-e LLAMA_API_KEY=...` if llama-server has one.

## The agent as a systemd service

For llama.cpp running as a systemd unit, or for anyone who would rather not
give a container the access the agent needs. From a checkout, as root:

```bash
deploy/install-agent.sh
```

It builds the agent into one file, checks it, and installs
`inferenceinquire-agent.service`. It listens on 127.0.0.1:9101 and prints where
its token is (`/etc/inferenceinquire/agent-token`). The agent finds llama*
systemd units, and llama.cpp containers too.

Then run the dashboard on the same machine:

```bash
echo "AGENT_TOKEN=$(sudo cat /etc/inferenceinquire/agent-token)" >> .env
docker compose up -d dashboard
```

**A dashboard on another machine.** Let the agent listen on this machine's
address, and only for the dashboard's:

```bash
deploy/install-agent.sh --listen 192.0.2.10 --allow 192.0.2.20
```

Then give the dashboard `AGENT_URL=http://192.0.2.10:9101` and the token. The
agent drops connections from any other address before reading them.

Re-running the script updates the agent and keeps its token. Settings go in
`/etc/inferenceinquire/agent.env`; see [configuration.md](configuration.md).

## Proxmox, with llama.cpp in an LXC container

For a Proxmox host where llama.cpp runs as a systemd unit inside a container.
The dashboard gets a container of its own, which must exist and run Docker
(`features: nesting=1,keyctl=1`).

On the Proxmox host, from a checkout, as root:

```bash
DASH_CTID=101 deploy/proxmox/install.sh
```

It:

1. installs the agent on the host, listening for the dashboard container's
   address only when that address is fixed;
2. asks the agent which container has a llama* unit, and reads that unit's API
   key file, if it has one;
3. copies the dashboard into `/opt/inferenceinquire` in the dashboard's
   container, writes its `.env` (mode 0600), builds the image and starts it;
4. enables everything to start at boot.

It never changes the inference container or any llama.cpp unit, and it is safe
to re-run after any change. Settings for your site go in `site.env` at the top
of the checkout (`KEY=VALUE` lines, not tracked by git); the installer adds
them to the dashboard's environment.

To deploy from another machine, `deploy/proxmox/sync.sh <ssh-host> <checkout>`
copies your working tree to the host's checkout with rsync and runs the
installer there.

## Running from source

```bash
pip install -r app/requirements.txt
cd app
LLAMA_URL=http://127.0.0.1:8080 python -m inferenceinquire   # or DEMO=1
```

The history goes to `/data/metrics.db` unless `DB_PATH` says otherwise.

## Upgrading

- Compose: `git pull && docker compose pull && docker compose up -d`. To stay
  on a version, set `TAG` in `.env`.
- `docker run`: pull the new image and recreate the container; the history is
  in the `inferenceinquire` volume.
- Agent service: `git pull`, then run `deploy/install-agent.sh` again.
- Proxmox: `git pull`, then run `deploy/proxmox/install.sh` again.

The database migrates itself on start.

## Removing it

```bash
docker compose down -v                            # compose, with its volumes
sudo systemctl disable --now inferenceinquire-agent
sudo rm -f /usr/local/bin/inferenceinquire-agent /etc/systemd/system/inferenceinquire-agent.service
sudo rm -rf /etc/inferenceinquire && sudo systemctl daemon-reload
```

On Proxmox, also stop the dashboard: `pct exec <ctid> -- systemctl disable --now
inferenceinquire`, and remove `/opt/inferenceinquire` in that container.
Nothing else was changed.
