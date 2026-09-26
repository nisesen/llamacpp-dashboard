# Alerts and notifications

Alerts show in the verdict line at the top of every page, as markers on every
chart, and in the event log under Health. Push notifications are optional.

Thresholds come from the hardware or from the loaded model's own history, not
from constants: a temperature limit is the card's own, a power limit is a
multiple of the card's own cap, and "too slow" is learned per model.

## Decode far below this model's norm

This is the failure the dashboard exists to catch. If the GPU drops out,
llama.cpp can fall back to the CPU without an error: the service stays up,
`/health` still answers 200, and generation runs ten or twenty times slower.

The dashboard learns each model's normal decode speed, as the median of its
last 300 requests, kept per model file. A rate under 25% of that median, three
requests in a row, raises a critical alert. Before a model has 12 requests to
learn from, three requests in a row under 5 tokens per second raise a serious
one ("Decode suspiciously slow"). Only requests that generated at least 24
tokens count: a short answer's rate is mostly overhead.

It takes three because one slow request usually means nothing. llama.cpp
charges each slot the wall time it spent, so a request that overlapped another
slot's long prefill reports a fraction of the real rate while the server is
fine. A CPU fallback makes every request slow, so it clears the bar within a
few requests.

## The other rules

| Alert | Level | When |
|---|---|---|
| Inference server unreachable, or unhealthy | critical | llama.cpp does not answer, or `/health` is not ok, for 20 s |
| No inference unit is running | critical | none of the llama.cpp units or containers is active |
| Two models are running at once | critical | two are active together |
| A unit restarted | warning | its restart count grew |
| A GPU has disappeared | critical | fewer cards than before |
| Xid faults | critical | NVIDIA Xid errors in the kernel log |
| Memory or core temperature | warning to critical | past the card's own slowdown and shutdown points |
| Power | warning, serious | over 1.15× or 1.35× the card's cap for about 12 s |
| Throttling | serious | thermal or hardware slowdown |
| PCIe link degraded | warning | a card below its maximum link generation under load |
| Remapped rows, uncorrectable ECC | serious, critical | growth since the card was first seen, so a used card's history is not an alert |
| New BMC event | warning to serious | a new record in the event log; serious for ECC, power supply and non-recoverable events |
| IPMI sensor | serious | outside its own critical band |
| Host memory, storage | warning | over 93% and 90% |
| Host agent unreachable | serious | the agent does not answer for 10 s |
| No inference server found | serious | the agent finds no llama.cpp to follow |
| Request telemetry degraded | warning | three requests finished in `/metrics` with no timings in the log: the log format probably changed |

Short spikes above a card's power cap are normal during prefill: cards enforce
their cap as a rolling average. The power rule waits for about 12 seconds over
it, which a card that is actually stuck over its cap will reach.

## Restarts and model switches

A model switch takes the server down on purpose. While one is visibly under way
(a unit starting, `/health` answering "Loading model", or the log mid-load),
the reachability alerts are held back and the page shows a "Restarting or
switching" banner. The event log gets one start and one end, with the time the
load took. A load that fails alerts as soon as the transition ends, and one
still going after `TRANSITION_MAX` (300 s) alerts as usual.

## Push notifications

Set `NOTIFY_URLS` to one or more [Apprise URLs](https://github.com/caronc/apprise/wiki),
separated by spaces:

```bash
NOTIFY_URLS=ntfys://ntfy.sh/your-topic tgram://BOT_TOKEN/CHAT_ID
```

Or set `NOTIFY_COMMAND` to a command that gets the message on stdin. Then send
a test message:

```bash
docker compose exec dashboard python -m inferenceinquire.notify --test
```

What is pushed: critical and serious alerts, and the warnings that tend to come
before hardware trouble (power, temperatures, restarts, BMC events). Each is
sent when it is raised, when it gets worse, and when it clears, several to a
message. An alert that clears and comes back is announced again at most every
30 minutes (`NOTIFY_COOLDOWN`), unless it is critical. What was announced is
kept in the database, so a restart repeats nothing.

If another monitor already reports that the server is unreachable, list those
alert keys in `PUSH_SKIP_KEYS` (for example `llama,llama_health,units`) so you
are not told twice.

## When the dashboard itself stops

A dashboard that is down sends nothing. Watch it from somewhere else: point an
uptime checker at `/healthz`, or run the forwarder in
[examples/forwarder](../examples/forwarder), which polls `/api/alerts` from
another machine, forwards the alerts, and says when the dashboard stops
answering or stops polling.

`GET /api/alerts?since=<unix time>` returns the current alerts, the
push-worthy raise and clear events since that time, and `poll_age`, the
seconds since the dashboard last polled.
