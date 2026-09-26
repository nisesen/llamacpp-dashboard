"""Completed requests: telling a health-check probe from real work, and what
the tiles show of one.
"""

from __future__ import annotations

from . import config


def is_probe(r: dict) -> bool:
    return (
        config.PROBE_GEN_TOKENS is not None
        and r.get("gen_tokens") == config.PROBE_GEN_TOKENS
        and (r.get("n_tokens") or 0) < config.PROBE_MAX_TOKENS
    )


def request_view(r: dict | None) -> dict | None:
    """The fields the tiles show for one completed request."""
    if not r:
        return None
    total_prompt = (r.get("n_tokens") or 0) - (r.get("gen_tokens") or 0)
    computed = r.get("prompt_tokens")
    reuse = None
    if computed is not None and total_prompt > 0:
        reuse = max(0.0, min(1.0, 1 - computed / total_prompt))
    return {
        "task": r.get("task"),
        "slot": r.get("slot"),
        "finished": r.get("finished"),
        "decode_tps": r.get("decode_tps"),
        "gen_tokens": r.get("gen_tokens"),
        "prefill_tps": r.get("prefill_tps")
        if (computed or 0) >= config.MIN_PREFILL_TOKENS
        else None,
        "prompt_tokens": computed,
        "n_tokens": r.get("n_tokens"),
        "accept_rate": r.get("accept_rate"),
        "reuse": reuse,
        "cold": (computed or 0) >= config.COLD_PREFILL_TOKENS,
    }
