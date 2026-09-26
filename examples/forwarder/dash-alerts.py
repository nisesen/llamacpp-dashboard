#!/usr/bin/env python3
"""
Forward InferenceInquire's alerts to a chat, from another machine.

Runs on any machine that can reach the dashboard, from a timer every minute.
It keeps no rules of its own: the dashboard decides what is push-worthy
(`notify` on each alert in /api/alerts). This turns the current alerts into
"raised" and "cleared" messages, catches conditions that came and went between
two runs from the dashboard's event log, and says so when the dashboard itself
stops answering or stops polling - because then nothing is being watched.

If another monitor already reports reachability from its own side, list those
alert keys in the dashboard's PUSH_SKIP_KEYS so nothing arrives twice.

Configuration (environment; the systemd unit reads ~/.config/dash-alerts.env):

  DASH_URL    dashboard base URL, e.g. http://192.0.2.10             (required)
  DASH_SEND   shell command that sends one message; it gets the text on
              stdin and in $DASH_MESSAGE, e.g.
                apprise -b "$DASH_MESSAGE" tgram://BOT_TOKEN/CHAT_ID
                curl -fsS -d @- https://ntfy.sh/your-topic          (required)
  DASH_STATE  state file (default ~/.local/state/dash-alerts.json)
  DRY_RUN=1   print instead of sending

Stdlib only.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

DASH = os.environ.get("DASH_URL", "").rstrip("/")
SEND = os.environ.get("DASH_SEND", "")
STATE = os.environ.get("DASH_STATE", os.path.expanduser("~/.local/state/dash-alerts.json"))
DRY = os.environ.get("DRY_RUN") == "1"

DOWN_TRIP = 3  # consecutive failed polls (~3 min) before saying so
STALL_AFTER = 60.0  # s since the dashboard's last poll: answering, not watching
# A condition that clears and comes back is not news every time. Criticals
# always are; anything else re-announces at most this often.
COOLDOWN = 1800.0
MAX_LINES = 12

ICON = {"critical": "🔴", "serious": "🟠", "warning": "🟡"}
RANK = {"warning": 1, "serious": 2, "critical": 3}


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


def send(text):
    if DRY:
        print("---- would send ----\n" + text)
        return True
    try:
        r = subprocess.run(
            SEND,
            shell=True,
            input=text,
            text=True,
            capture_output=True,
            timeout=60,
            env={**os.environ, "DASH_MESSAGE": text},
        )
    except Exception:
        return False
    return r.returncode == 0


def line(a, suffix=""):
    detail = (a.get("detail") or "").strip()
    return (
        f"{ICON.get(a.get('level'), '•')} {a.get('title', '?')}"
        + (f" — {detail[:220]}" if detail else "")
        + suffix
    )


def main():
    if not DASH or not (SEND or DRY):
        print("dash-alerts: set DASH_URL and DASH_SEND (see the docstring)", file=sys.stderr)
        return 2
    now = time.time()
    st = load_state()
    first_run = "since" not in st
    st.setdefault("since", now)  # never replay history on first run
    st.setdefault("fail", 0)
    st.setdefault("open", {})  # key -> {title, level}: announced, not yet cleared
    st.setdefault("last", {})  # key -> when last announced

    try:
        with urllib.request.urlopen(f"{DASH}/api/alerts?since={st['since']}", timeout=10) as r:
            d = json.load(r)
    except Exception as exc:
        st["fail"] += 1
        if (
            st["fail"] >= DOWN_TRIP
            and not st.get("down_sent")
            and send(
                f"⚪ InferenceInquire unreachable ({DASH}) for {st['fail']} checks: {exc}.\n"
                f"GPU, power and BMC alerts are NOT being forwarded until it is back."
            )
        ):
            st["down_sent"] = True
        save_state(st)
        return 0

    msgs = []
    if st.get("down_sent"):
        msgs.append("🟢 InferenceInquire reachable again — alert forwarding resumed.")
        st["down_sent"] = False
    st["fail"] = 0

    age = d.get("poll_age")
    if age is not None and age > STALL_AFTER:
        if not st.get("stall_sent"):
            msgs.append(
                f"⚪ InferenceInquire answers but has not polled for {age:.0f}s — "
                f"its alerts are stale."
            )
            st["stall_sent"] = True
    elif st.get("stall_sent"):
        msgs.append("🟢 InferenceInquire polling again.")
        st["stall_sent"] = False

    current = {a["key"]: a for a in d.get("alerts", []) if a.get("notify")}
    lines = []

    def may_announce(key, level):
        return level == "critical" or now - st["last"].get(key, 0) >= COOLDOWN

    # Came and went since the last run: only the event log saw it.
    if not first_run:
        seen = set()
        for e in d.get("events", []):
            k = e["key"]
            if e.get("state") != "raised" or k in current or k in st["open"] or k in seen:
                continue
            seen.add(k)
            if may_announce(k, e.get("level")):
                lines.append(line(e, " (already cleared)"))
                st["last"][k] = now

    # Raised, or escalated.
    for k, a in current.items():
        was = st["open"].get(k)
        if was is None:
            if may_announce(k, a.get("level")):
                lines.append(line(a))
                st["open"][k] = {"title": a.get("title"), "level": a.get("level")}
                st["last"][k] = now
        elif RANK.get(a.get("level"), 0) > RANK.get(was.get("level"), 0):
            lines.append(line(a, " (escalated)"))
            st["open"][k] = {"title": a.get("title"), "level": a.get("level")}
            st["last"][k] = now

    # Cleared.
    for k in list(st["open"]):
        if k not in current:
            lines.append(f"🟢 cleared: {st['open'][k].get('title', k)}")
            del st["open"][k]

    if lines:
        extra = len(lines) - MAX_LINES
        body = "\n".join(lines[:MAX_LINES]) + (f"\n…and {extra} more" if extra > 0 else "")
        msgs.append(f"InferenceInquire alerts\n{body}\n{DASH}/")

    ok = all(send(m) for m in msgs) if msgs else True
    if ok:
        events = d.get("events", [])
        st["since"] = max([st["since"]] + [e["ts"] for e in events])
        save_state(st)
    # On a failed send the state is not saved: the next run retries the same
    # edges rather than dropping them.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never let the timer unit go red silently
        print(f"dash-alerts: {exc}", file=sys.stderr)
        sys.exit(1)
