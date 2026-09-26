"""
InferenceInquire: the dashboard's backend.

Polls two sources on a fixed cadence and fans the result out over a WebSocket:

  * the read-only host agent  (GPU, CPU, RAM, IPMI, guests, Xid, discovery)
  * llama.cpp                 (/metrics, /props, /slots, /health)

Two things are worth knowing before reading the rest.

**Counters, not rates.** llama.cpp exposes cumulative counters. Live decode
tok/s, prefill tok/s, speculative acceptance and prompt-cache hit rate are all
*differences* of those counters between polls. Its `predicted_tokens_seconds`
gauge is NOT a lifetime average - llama.cpp resets that bucket on every scrape -
so lifetime figures are computed from the `_total` counters instead.

**Nothing about the machine is hardcoded.** Temperature ceilings and power
caps come from the cards themselves via the agent, keyed by serial. The
"too slow" threshold is *learned* per model rather than fixed, because a
number that is right for one model is wrong for the next one loaded. Anything
site-specific (the probe fingerprint, what an external watchdog already
covers) is configuration, not code.
"""

# The release, shared with the agent (its config.AGENT_VERSION) and the tag;
# tests/test_version.py and CI keep them in step.
__version__ = "0.1.0"
