# Security

InferenceInquire only reads. The dashboard never changes llama.cpp, the host
or the GPUs, and the agent runs queries only. But what it shows is sensitive
enough to protect, and the agent needs real access to read it.

## What the page shows

GPU serial numbers, addresses, guest and container names, model file paths,
request sizes and timings, and the alert history. It never sees prompts or
answers. The page has **no login**, and it answers anyone who can reach its
port.

Keep it on a network you trust. For anything wider, put it behind a proxy that
adds TLS and authentication, and never expose it directly to the internet.

### Caddy

Automatic HTTPS and a password. Generate the hash with `caddy hash-password`:

```
dashboard.example.com {
    basic_auth {
        admin $2a$14$...your-hash...
    }
    reverse_proxy 127.0.0.1:8000
}
```

With the dashboard behind a proxy on the same machine, set
`LISTEN_ADDRESS=127.0.0.1` so it cannot be reached around the proxy.

### Tailscale

`tailscale serve` publishes the dashboard to your tailnet only, over HTTPS,
with Tailscale's identity in front:

```bash
tailscale serve --bg 8000
```

This also makes the page installable as an app on Android, which needs HTTPS.

## The agent

- It answers only with a bearer token, kept in a file readable by root
  (`/etc/inferenceinquire/agent-token`, mode 0600) or, with `compose.yaml`, in
  a volume shared with the dashboard alone.
- It speaks plain HTTP, so the token is visible to anyone who can watch the
  traffic between the dashboard and the agent. Keep that path on the same
  machine or a trusted network.
- It listens on 127.0.0.1 only unless told otherwise. When the dashboard runs elsewhere,
  let it listen on one address and allow only the dashboard's
  (`deploy/install-agent.sh --listen ... --allow ...`, or `AGENT_ALLOW`): any
  other address is dropped before a byte is read. The Proxmox installer sets
  this up for the dashboard's container when its address is fixed.

### What the agent container is given

In `compose.yaml` the agent runs as root, with a read-only filesystem and no
new privileges, and:

- the host's process namespace (`pid: host`), to see which llama-server is
  running and read its model file's header;
- the GPUs, through the NVIDIA Container Toolkit, for `nvidia-smi`;
- `CAP_SYSLOG`, to read GPU faults from the kernel log;
- the Docker socket, to find llama.cpp's container and follow its log.

The Docker socket is the strongest of these: whoever holds it controls Docker.
The agent only ever sends GET requests, but the socket does not enforce that.
To make sure of it, run a proxy that allows only reading containers, such as
[docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) with
`CONTAINERS=1`, and point the agent at it with
`DOCKER_HOST=tcp://127.0.0.1:2375` instead of mounting the socket. If you
would rather not give a container any of this, run the agent as a systemd
service instead (`deploy/install-agent.sh`).

## The dashboard container

It runs as an unprivileged user (uid 10001) with a read-only filesystem, and
writes only its database and, with `compose.yaml`, the agent's token. Its
settings, which may hold llama.cpp's API key and notification URLs with bot
tokens in them, live in `.env`; keep that file readable only by you. The
Proxmox installer writes it with mode 0600.

## Reporting a vulnerability

Please report it privately, as [SECURITY.md](../SECURITY.md) describes, not
in a public issue.
