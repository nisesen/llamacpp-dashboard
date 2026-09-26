"""
InferenceInquire's host agent: read-only telemetry for the machine behind
the LLM server.

Serves one JSON document describing GPU, CPU, memory, disk, network, IPMI and
guest state. Every external command is a query; nothing here mutates the box.

Design rule: **discover, don't declare.** The machine is an experiment, so the
agent is told as little as possible and works the rest out at runtime -
which container runs llama.cpp, what its units are called, which NIC carries
traffic, and what each GPU's own temperature and power limits are. Swapping a
card or adding a third model should need no edit here.

  GET /metrics.json    Authorization: Bearer <token>
  GET /healthz         no auth
Stdlib only. It runs as one file built from this package by agent/build.sh,
on the host's own python3 (3.11 or newer; deploy/install-agent.sh), or as a
container (agent/Dockerfile).
"""
