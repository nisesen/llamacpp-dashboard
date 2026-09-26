"""The loaded model's architecture, read from its GGUF header."""

from ..cache import CACHES
from ..runtimes import read_head

# Minimal GGUF header reader. The architecture is metadata in the file, so the
# layer count, attention layout and MoE shape are facts we can read rather
# than guess - and they change when the model does.
(
    _G_U8,
    _G_I8,
    _G_U16,
    _G_I16,
    _G_U32,
    _G_I32,
    _G_F32,
    _G_BOOL,
    _G_STR,
    _G_ARR,
    _G_U64,
    _G_I64,
    _G_F64,
) = range(13)


def _g_read(buf, pos, fmt):
    import struct

    n = struct.calcsize(fmt)
    if pos + n > len(buf):
        raise EOFError
    return struct.unpack_from(fmt, buf, pos)[0], pos + n


def _g_str(buf, pos):
    n, pos = _g_read(buf, pos, "<Q")
    if pos + n > len(buf):
        raise EOFError
    return buf[pos : pos + n].decode("utf-8", "replace"), pos + n


def _g_val(buf, pos, t, keep_array=False):
    simple = {
        _G_U8: "<B",
        _G_I8: "<b",
        _G_U16: "<H",
        _G_I16: "<h",
        _G_U32: "<I",
        _G_I32: "<i",
        _G_F32: "<f",
        _G_U64: "<Q",
        _G_I64: "<q",
        _G_F64: "<d",
    }
    if t in simple:
        return _g_read(buf, pos, simple[t])
    if t == _G_BOOL:
        v, pos = _g_read(buf, pos, "<B")
        return bool(v), pos
    if t == _G_STR:
        return _g_str(buf, pos)
    if t == _G_ARR:
        et, pos = _g_read(buf, pos, "<I")
        n, pos = _g_read(buf, pos, "<Q")
        vals = []
        for i in range(n):
            v, pos = _g_val(buf, pos, et)
            if keep_array and i < 256:
                vals.append(v)
        return (vals if keep_array else f"<{n} items>"), pos
    raise ValueError(t)


def parse_gguf_header(buf):
    """Pull architecture metadata out of a GGUF header block."""
    if buf[:4] != b"GGUF":
        return {}
    pos = 4
    _ver, pos = _g_read(buf, pos, "<I")
    _nt, pos = _g_read(buf, pos, "<Q")
    n_kv, pos = _g_read(buf, pos, "<Q")
    meta = {}
    wanted_arrays = ("compress_ratios",)
    try:
        for _ in range(n_kv):
            k, pos = _g_str(buf, pos)
            t, pos = _g_read(buf, pos, "<I")
            keep = any(w in k for w in wanted_arrays)
            v, pos = _g_val(buf, pos, t, keep_array=keep)
            meta[k] = v
    except (EOFError, ValueError):
        meta["_truncated"] = True
    return meta


def active_model_path():
    """model_path of the unit serving right now, from the guest cache."""
    guests, _ = CACHES["guests"].get()
    ct = (guests or {}).get("llm_ct") or {}
    unit = ct.get("active_unit")
    return (ct.get("units", {}).get(unit) or {}).get("model_path") if unit else None


def collect_model_arch():
    """Architecture of whichever model is loaded, read from its GGUF header."""
    guests, _ = CACHES["guests"].get() if "guests" in CACHES else ({}, 0)
    ct = (guests or {}).get("llm_ct") or {}
    ctid = ct.get("vmid")
    unit = ct.get("active_unit")
    path = (ct.get("units", {}).get(unit) or {}).get("model_path") if unit else None
    if not (ctid and path):
        return {"available": False}

    # The KV block is dominated by the tokenizer array - a full header is
    # ~16 MB on these models. The architecture keys sit before it, so read a
    # cheap prefix first and only escalate if they are not there.
    meta = {}
    for size in (4_000_000, 16_000_000):
        out = read_head(ctid, unit, path, size)
        if not out:
            return {"available": False, "path": path}
        meta = parse_gguf_header(out)
        arch_probe = meta.get("general.architecture")
        if arch_probe and meta.get(f"{arch_probe}.block_count") is not None:
            break
    if not meta:
        return {"available": False, "path": path}
    return arch_from_meta(meta, path)


def arch_from_meta(meta, path):
    """Map GGUF metadata onto the fields the dashboard draws.

    Pure, so `--self-test` can run it against every model on disk and prove the
    anatomy panel copes with dense models, uniform attention stacks and plain
    multi-head attention - not just whatever happens to be loaded.
    """
    arch = meta.get("general.architecture") or ""
    g = lambda suffix, default=None: meta.get(f"{arch}.{suffix}", default)  # noqa: E731
    ratios = g("attention.compress_ratios") or []
    n_layer = g("block_count")
    interval = g("full_attention_interval")

    # Which blocks are full attention: from the per-layer array when present,
    # otherwise from the declared interval.
    full = []
    if isinstance(ratios, list) and ratios:
        full = [i for i, v in enumerate(ratios) if v]
    elif n_layer and interval:
        full = [i for i in range(n_layer) if (i + 1) % interval == 0]

    n_head = g("attention.head_count")
    n_head_kv = g("attention.head_count_kv")
    return {
        "available": True,
        "header_truncated": bool(meta.get("_truncated")),
        "kv_pairs_read": len([k for k in meta if not k.startswith("_")]),
        "path": path,
        "file": path.rsplit("/", 1)[-1],
        "architecture": arch,
        "name": meta.get("general.name"),
        "n_layer": n_layer,
        "n_embd": g("embedding_length"),
        "n_head": n_head,
        # Absent means multi-head attention: every query head has its own KV
        # head. Defaulting to 1 would print a wildly wrong GQA ratio.
        "n_head_kv": n_head_kv if n_head_kv is not None else n_head,
        "gqa_reported": n_head_kv is not None,
        "key_length": g("attention.key_length"),
        "value_length": g("attention.value_length"),
        "context_length": g("context_length"),
        "expert_count": g("expert_count"),
        "expert_used": g("expert_used_count"),
        "expert_ffn": g("expert_feed_forward_length"),
        "shared_ffn": g("expert_shared_feed_forward_length"),
        "full_attention_interval": interval,
        "full_attention_layers": full,
        "compress_ratios": ratios if isinstance(ratios, list) else [],
        "indexer_heads": g("attention.indexer.head_count"),
        "indexer_top_k": g("attention.indexer.top_k"),
        "rope_freq_base": g("rope.freq_base"),
    }
