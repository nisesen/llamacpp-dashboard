# Roadmap

What is planned, roughly in the order it will be done. Each item has an
[issue labelled `roadmap`](https://github.com/nisesen/inferenceinquire/issues?q=label%3Aroadmap),
where the details and the discussion live. Suggestions are welcome there.

## Next

- **Slow-decode alerts under heavy load.** When every slot is busy with long
  prompts, one request can read slow three times in a row while the server is
  healthy. Judge a slow reading against the total across the slots running at
  the same moment.
- **AMD, Intel and Apple GPUs.** Power, temperature, utilisation and memory
  from each vendor's own tools or sysfs, with NVIDIA-only panels hidden.
- **Several models behind one endpoint**: llama.cpp's router mode and
  llama-swap, each model as its own labelled series.
- **Mute and acknowledge alerts** from the page, and a read-only page of the
  settings in effect.
- **A Grafana dashboard** for the `/metrics` export, and an optional compose
  profile with VictoriaMetrics and Grafana.
- **Backups of the history database.**
- **A heartbeat to an external monitor** (Healthchecks, Uptime Kuma), so a
  stopped dashboard is noticed without running the example forwarder.
- **Which client sent a request.** llama.cpp does not log it; it would need a
  proxy in front of the server, which is worth deciding carefully.

## Not planned

- **NVIDIA DCGM.** It needs NVIDIA's DCGM service, and `nvidia-smi` in loop
  mode is already cheap.
- **Prompt-level tracing** (Langfuse, OpenLIT, Phoenix). Too heavy for a
  single server, and it stores prompts.
- **A log pipeline** (Loki, Vector). The agent's log follower is enough.
