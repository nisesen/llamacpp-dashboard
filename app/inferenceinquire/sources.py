"""The two things the dashboard reads: the host agent and llama.cpp.

Both are read over HTTP, once per poll. A source that does not answer comes
back as a dict that says so, never as an exception: an unreachable server is
something to show, not a crash.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any

import httpx

from . import config

log = logging.getLogger(__name__)

METRICS_SLEEP_SAFE_BUILD = 10519


def should_poll_slots(
    mode: str, sleep_idle_s: int | None, props: dict | None, metrics: dict | None
) -> bool:
    """Whether /slots can be read this poll without keeping the model awake."""
    if (props or {}).get("is_sleeping"):
        return False
    if mode == "always":
        return True
    if mode != "busy" and not (sleep_idle_s and sleep_idle_s > 0):
        return True
    m = metrics or {}
    return (m.get("llamacpp:requests_processing") or 0) > 0 or (
        m.get("llamacpp:requests_deferred") or 0
    ) > 0


def build_number(build_info: str | None) -> int | None:
    """llama.cpp's build number from /props build_info, e.g. 'b10935-8e33095'."""
    head = (build_info or "").lstrip("b").split("-", 1)[0]
    return int(head) if head.isdigit() else None


def parse_prometheus(text: str) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Return (plain metrics, labelled metrics). Good enough for llama.cpp."""
    plain: dict[str, float] = {}
    labelled: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        name = name.strip()
        if not name:
            continue
        try:
            val = float(value)
        except ValueError:
            continue
        if "{" in name:
            base, _, rest = name.partition("{")
            key = rest.rstrip("}").split("=", 1)[-1].strip('"') if "=" in rest else rest
            labelled.setdefault(base, {})[key] = val
        else:
            plain[name] = val
    return plain, labelled


_token: dict[str, str] = {}


def agent_token() -> str:
    """AGENT_TOKEN, or the one in AGENT_TOKEN_FILE - made there if it is not.

    The agent reads its token file on every request, so a token this creates
    is honoured as soon as it is written: the dashboard and a containerised
    agent need nothing but a shared volume.
    """
    if config.AGENT_TOKEN or not config.AGENT_TOKEN_FILE:
        return config.AGENT_TOKEN
    if "file" not in _token:
        path = config.AGENT_TOKEN_FILE
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(24) + "\n")
            log.info("created the agent token in %s", path)
        with open(path) as f:
            _token["file"] = f.read().strip()
    return _token["file"]


async def fetch_agent(client: httpx.AsyncClient) -> dict:
    try:
        r = await client.get(
            f"{config.AGENT_URL}/metrics.json",
            headers={"Authorization": f"Bearer {agent_token()}"},
            timeout=8.0,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"agent_ok": False, "error": str(exc)}


async def fetch_llama(client: httpx.AsyncClient, url: str, sleep_idle_s: int | None = None) -> dict:
    headers = {"Authorization": f"Bearer {config.LLAMA_KEY}"} if config.LLAMA_KEY else {}
    out: dict[str, Any] = {"reachable": False, "url": url}
    if not url:
        out["error"] = "no endpoint discovered yet"
        return out
    t0 = time.perf_counter()
    try:
        r = await client.get(f"{url}/health", timeout=6.0)
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["reachable"] = True
        try:
            body = r.json()
        except ValueError:
            body = {}
        err = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
        out["health_status"] = body.get("status") or err or f"HTTP {r.status_code}"
        out["healthy"] = body.get("status") == "ok"
        # While weights load, llama.cpp answers 503 "Loading model". That is
        # a switch or restart in progress, not an unhealthy server.
        out["loading"] = r.status_code == 503 and "load" in (err or "").lower()
    except Exception as exc:
        out["error"] = str(exc)
        return out

    async def get(path, parser):
        try:
            r = await client.get(f"{url}{path}", headers=headers, timeout=6.0)
            r.raise_for_status()
            return parser(r)
        except Exception:
            return None

    out["metrics"] = await get("/metrics", lambda r: parse_prometheus(r.text))
    out["props"] = await get("/props", lambda r: r.json())
    # Last, and only when it cannot keep the model awake: see SLOTS_POLL.
    out["slots_polled"] = should_poll_slots(
        config.SLOTS_POLL, sleep_idle_s, out["props"], (out["metrics"] or ({}, {}))[0]
    )
    out["slots"] = await get("/slots", lambda r: r.json()) if out["slots_polled"] else None
    return out
