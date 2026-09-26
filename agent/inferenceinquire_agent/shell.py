"""Running a command and reading numbers out of its output."""

import re
import subprocess

NA_VALUES = {
    "[N/A]",
    "N/A",
    "[Not Supported]",
    "Not Supported",
    "",
    "[Unknown Error]",
    "Unknown Error",
}


def run(cmd, timeout=15):
    """Run a command, returning stdout or None. Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    return p.stdout if p.returncode == 0 else None


def run_bytes(cmd, timeout=40):
    """Like run(), but returns raw bytes - GGUF headers are not text."""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        return None
    return p.stdout if p.returncode == 0 else None


def num(value):
    if value is None or value in NA_VALUES:
        return None
    try:
        f = float(value)
    except (ValueError, TypeError):
        return None
    return int(f) if f.is_integer() else f


def xml_num(node, tag):
    """Pull a number out of nvidia-smi XML, which suffixes units ('105 C')."""
    if node is None:
        return None
    text = node.findtext(tag)
    if text is None or text.strip() in NA_VALUES:
        return None
    m = re.match(r"\s*(-?[\d.]+)", text)
    return num(m.group(1)) if m else None
