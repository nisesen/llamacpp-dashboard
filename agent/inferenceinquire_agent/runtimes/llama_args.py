"""Reading llama-server's own settings from its command line."""

import re

from .. import config
from ..shell import num

EXEC_PORT = re.compile(r"--port\s+(\d+)")
EXEC_HOST = re.compile(r"--host\s+(\S+)")
EXEC_KEYFILE = re.compile(r"--api-key-file\s+(\S+)")
EXEC_MODEL = re.compile(r"(?:--model|-m)\s+(\S+)")
EXEC_ALIAS = re.compile(r"--alias\s+(\S+)")
EXEC_CACHE_RAM = re.compile(r"(?:--cache-ram|-cram)\s+(-?\d+)")
# --sleep-idle-seconds: the server unloads an idle model and frees its memory.
# The dashboard needs to know, because some of llama.cpp's endpoints count as
# activity and would keep it awake (see the dashboard's should_poll_slots).
EXEC_SLEEP = re.compile(r"--sleep-idle-seconds\s+(-?\d+)")


def settings_from_args(args):
    """What the dashboard needs to know from a llama-server command line."""
    port = EXEC_PORT.search(args)
    model = EXEC_MODEL.search(args)
    alias = EXEC_ALIAS.search(args)
    cram = EXEC_CACHE_RAM.search(args)
    sleep = EXEC_SLEEP.search(args)
    keyfile = EXEC_KEYFILE.search(args)
    host = EXEC_HOST.search(args)
    return {
        "port": num(port.group(1)) if port else None,
        "model_path": model.group(1) if model else None,
        "alias": alias.group(1) if alias else None,
        # -1 = unlimited, 0 = disabled, else MiB.
        "cache_ram_mib": int(cram.group(1)) if cram else config.CACHE_RAM_DEFAULT,
        "cache_ram_set": bool(cram),
        # None = not set (llama.cpp's default, -1, never sleeps).
        "sleep_idle_s": int(sleep.group(1)) if sleep else None,
        "key_file": keyfile.group(1) if keyfile else None,
        "bind": host.group(1) if host else None,
    }


# llama-server reads most of its options from LLAMA_ARG_* variables too, which
# is how container images are often configured.
ENV_ARGS = {
    "LLAMA_ARG_PORT": "--port",
    "LLAMA_ARG_HOST": "--host",
    "LLAMA_ARG_MODEL": "--model",
    "LLAMA_ARG_ALIAS": "--alias",
}


def args_with_env(argv, env):
    """A command line with the LLAMA_ARG_* settings it does not repeat added."""
    args = " ".join(argv)
    for var, flag in ENV_ARGS.items():
        value = env.get(var)
        if value and not re.search(rf"{flag}\s", args):
            args += f" {flag} {value}"
    return args


def loopback_url(bind, port):
    """Where a server bound to `bind` answers from this host."""
    host = bind if bind and bind not in ("0.0.0.0", "::", "[::]") else "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"
