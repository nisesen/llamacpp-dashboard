"""The log lines the parser recognises. llama.cpp's log format is not an API:
any build can change it, which is why each pattern is checked in selftest.py.
"""

import re

RE_TG = re.compile(
    r"slot print_timing: id\s+(\d+) \| task\s+(-?\d+) \| n_gen\s*=\s*(\d+), "
    r"tg\s*=\s*([\d.]+) t/s, tg_3s\s*=\s*([\d.]+) t/s"
)
RE_PROMPT = re.compile(
    r"slot print_timing: id\s+(\d+) \| task\s+(-?\d+) \|\s*prompt eval time\s*=\s*"
    r"([\d.]+) ms /\s*(\d+) tokens \(\s*([\d.]+) ms per token,\s*([\d.]+) tokens per second\)"
)
RE_EVAL = re.compile(
    r"slot print_timing: id\s+(\d+) \| task\s+(-?\d+) \|\s*eval time\s*=\s*"
    r"([\d.]+) ms /\s*(\d+) tokens \(\s*([\d.]+) ms per token,\s*([\d.]+) tokens per second\)"
)
RE_TOTAL = re.compile(
    r"slot print_timing: id\s+(\d+) \| task\s+(-?\d+) \|\s*total time\s*=\s*"
    r"([\d.]+) ms /\s*(\d+) tokens"
)
RE_DRAFT = re.compile(
    r"slot print_timing: id\s+(\d+) \| task\s+(-?\d+) \| draft acceptance\s*=\s*([\d.]+) "
    r"\(\s*(\d+) accepted /\s*(\d+) generated\), mean len\s*=\s*([\d.]+)"
)
RE_GRAPHS = re.compile(
    r"slot print_timing: id\s+(\d+) \| task\s+(-?\d+) \|\s*graphs reused\s*=\s*(\d+)"
)
RE_LAUNCH = re.compile(
    r"slot launch_slot_: id\s+(\d+) \| task\s+(-?\d+) \| processing task, is_child = (\d+)"
)
RE_RELEASE = re.compile(
    r"slot\s+release: id\s+(\d+) \| task\s+(-?\d+) \| stop processing: "
    r"n_tokens = (\d+), truncated = (\d+)"
)
RE_PICK_LRU = re.compile(
    r"slot get_availabl: id\s+(\d+) \|.*selected slot by LRU, t_last = (-?\d+)"
)
RE_PICK_LCP = re.compile(
    r"slot get_availabl: id\s+(\d+) \|.*selected slot by LCP similarity, "
    r"f_sim_best = ([\d.]+).*f_keep = ([\d.]+)"
)
RE_LOADING = re.compile(r"srv\s+load_model: loading model '([^']+)'")
RE_LOADED = re.compile(r"srv\s+llama_server: model loaded")
RE_LISTEN = re.compile(r"srv\s+llama_server: listening on (\S+)")
RE_OOM = re.compile(
    r"(cudaMalloc failed: out of memory|failed to allocate|"
    r"retrying without pipeline parallelism)"
)
# Signals /metrics does not carry. llama.cpp does not log the client address,
# so a rejected key can be counted here but not attributed.
RE_AUTH_FAIL = re.compile(r"unauthorized: Invalid API Key")
# The host-RAM prompt cache: an idle slot's state is parked here and restored
# on a matching request. Evicting an entry, or skipping one as too large, is
# what turns the next turn of a long conversation into a full re-prefill.
RE_CACHE_EVICT = re.compile(
    r"making room for prompt cache entry, removing oldest entry \(size = ([\d.]+) MiB\)"
)
RE_CACHE_SKIP = re.compile(r"prompt state size ([\d.]+) MiB exceeds cache size limit ([\d.]+) MiB")
RE_CANCEL = re.compile(r"srv\s+stop: cancel task, id_task = (\d+)")
