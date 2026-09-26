# Reading the numbers

## Where each figure comes from

Nothing about the machine is written into the code. The dashboard asks.

| What | Where it comes from |
|---|---|
| Where llama.cpp runs | the agent: a Docker container, a systemd unit, or a Proxmox container that runs llama-server |
| Ports, model, alias, API key file, `--cache-ram`, `--sleep-idle-seconds` | the server's own command line (and `LLAMA_ARG_*` variables in a container) |
| The endpoint to poll | the running server's address and port, unless `LLAMA_URL` is set |
| GPU temperature limits | each card, through `nvidia-smi -q -x`, keyed by serial |
| GPU power caps | each card's own current limit; the alerts are multiples of it |
| BMC sensor limits | each sensor's own thresholds |
| The network interface to graph | the one that carries the default route |
| "Too slow to be right" | learned per model, see [alerts.md](alerts.md) |
| The model's shape | the loaded model's GGUF header |

Add a GPU, change a card's power cap, rename a unit or load another model, and
the dashboard follows without a setting. Limits are keyed by card serial, so a
replaced card brings its own.

## Metrics that mislead

### Throughput is counted per request, not continuously

llama.cpp adds a
request's generation time to its counters only when the request finishes, so
its `_total` counters move in steps. Decode, prefill, acceptance and prompt
reuse are therefore drawn as marks, one per request, never as lines, which
would suggest a continuous rate nobody measured. Without the agent, each mark
is a poll in which requests finished, averaged over them. Figures sampled
continuously (requests running, context, GPU, CPU, temperatures) are lines.

### Two gauges reset when read

`llamacpp:predicted_tokens_seconds` and
`llamacpp:prompt_tokens_seconds` are not averages over the server's life:
llama.cpp resets them on every scrape of `/metrics`. The dashboard computes
lifetime figures from the `_total` counters instead. If something else also
scrapes `/metrics`, each resets the gauges for the other.

### `model_ftype` can be wrong

`/props` can report a mixed-precision quant as
something else entirely: a Q8_K_XL file as `Q4_K - Medium`. The dashboard
shows the model's file path, which is trustworthy. llama.cpp also ignores the
model name a client asks for and serves whatever is loaded.

### Slow requests on a busy server are normal

llama.cpp charges each slot the
wall time it spent, so a request that ran alongside another slot's long prefill
reports a small fraction of the server's real speed. Compare the total across
slots before suspecting the hardware.

## llama.cpp's sleep mode

With `--sleep-idle-seconds`, llama.cpp unloads an idle model and frees its
memory until the next request. Monitoring must not undo that.

- `/health`, `/props` and `/metrics` are safe to poll: llama.cpp does not count
  them as activity. For `/metrics` that holds from build b10519.
- `/slots` is not. Reading it resets the idle timer, and it wakes a sleeping
  model.

So the dashboard reads `/slots` only while requests are running, which keep the
model awake anyway, and never while it sleeps. It learns about the flag from
the agent; without the agent, set `SLOTS_POLL=busy`. A sleeping model shows as
"asleep", not as a fault. On a build older than b10519 the model card warns
that scraping `/metrics` keeps the model awake.

## Long-term history

Raw rows are kept for `RETAIN_DAYS` (14). Every five minutes, finished periods
are folded into 5-minute and hourly rollups, kept for 90 days and two years.
The rollups keep what the charts need: counts add up, peaks keep their maximum,
health keeps its minimum, and the rest is averaged, so a 90-day power chart
still shows the real peak draw.

- The 30d and 90d ranges read the 5-minute rollups, and 1y the hourly ones.
- The request log is folded per hour and model too. Past 14 days the
  throughput charts show hourly means instead of one mark per request.
- Two years of hourly rows is under 20,000 rows per table.

After a restart, ranges of an hour or less are filled from the stored history
until the live buffer catches up.

## Prometheus

The dashboard serves its own figures at `/metrics`, for Prometheus,
VictoriaMetrics or anything else that reads that format:

```yaml
scrape_configs:
  - job_name: inferenceinquire
    static_configs:
      - targets: ["your-machine:8000"]
```

| Metric | Labels |
|---|---|
| `llmdash_up`, `llmdash_agent_up` | |
| `llmdash_decode_tokens_per_second`, `llmdash_prefill_tokens_per_second` | |
| `llmdash_decode_baseline_tokens_per_second`, `llmdash_live_tokens_per_second` | |
| `llmdash_requests_processing`, `llmdash_context_tokens` | |
| `llmdash_alerts` | `level` |
| `llmdash_gpu_temp_celsius`, `llmdash_gpu_memory_temp_celsius` | `gpu`, `serial` |
| `llmdash_gpu_power_watts`, `llmdash_gpu_power_limit_watts` | `gpu`, `serial` |
| `llmdash_gpu_utilization_ratio`, `llmdash_gpu_memory_used_bytes` | `gpu`, `serial` |
| `llmdash_gpu_pcie_rx_megabytes_per_second`, `..._tx_...` | `gpu`, `serial` |
| `llmdash_disk_read_bytes_per_second` | |
| `llmdash_journal_lines_matched_total` | `pattern` |

The last one counts the log lines each pattern has matched, which shows when a
llama.cpp build changes its log format.
