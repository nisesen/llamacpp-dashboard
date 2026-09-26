"""Push notifications: alerts sent out as they are raised, escalated and cleared.

NOTIFY_URLS takes Apprise URLs, separated by spaces or commas: ntfy, Telegram,
Discord, Slack, email, Pushover, a JSON webhook and a hundred more
(https://github.com/caronc/apprise/wiki). NOTIFY_COMMAND runs a command
instead, or as well, with the message on stdin and the title in
$NOTIFY_TITLE.

What is sent is what /api/alerts marks `notify` (alerts.should_notify: the
level, the warnings that precede hardware trouble, PUSH_SKIP_KEYS), after the
rules' hold times and model-switch transitions:
- one message per poll that has news: raised, escalated, cleared;
- a condition that clears and comes back is re-announced at most once per
  NOTIFY_COOLDOWN, unless it is critical;
- what was announced is kept in the store, so a restart repeats nothing.
  For a short while after start nothing is called cleared, because alerts
  wait out their hold times again before they reappear.

A dashboard cannot report its own outage: watch /healthz, or /api/alerts'
poll_age, from somewhere else for that.

  python -m inferenceinquire.notify --test    sends a test message
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import sys
import time

from . import config

log = logging.getLogger(__name__)

ICON = {"critical": "🔴", "serious": "🟠", "warning": "🟡"}
RANK = {"warning": 1, "serious": 2, "critical": 3}
MAX_LINES = 12
STATE_KEY = "notify_state"
# A message that could not be delivered is retried this often, for this long.
RETRY_EVERY = 60.0
GIVE_UP_AFTER = 3600.0


def line(a: dict, suffix: str = "") -> str:
    detail = (a.get("detail") or "").strip()
    return (
        f"{ICON.get(a.get('level'), '•')} {a.get('title', '?')}"
        + (f" — {detail[:220]}" if detail else "")
        + suffix
    )


class Sender:
    """Delivers one message to every configured destination."""

    def __init__(self, urls: list[str], command: str):
        self.urls, self.command = urls, command
        self._apprise = None

    def _via_apprise(self, title: str, body: str) -> bool:
        if self._apprise is None:
            import apprise  # only needed when NOTIFY_URLS is set

            self._apprise = apprise.Apprise()
            for url in self.urls:
                if not self._apprise.add(url):
                    log.warning("NOTIFY_URLS: not a URL Apprise understands: %s", url.split(":")[0])
        return bool(self._apprise.notify(title=title, body=body))

    def _via_command(self, title: str, body: str) -> bool:
        try:
            r = subprocess.run(
                self.command,
                shell=True,
                input=body,
                text=True,
                capture_output=True,
                timeout=60,
                env={**os.environ, "NOTIFY_TITLE": title},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("NOTIFY_COMMAND failed: %s", exc)
            return False
        if r.returncode:
            log.warning("NOTIFY_COMMAND exited %s: %s", r.returncode, r.stderr.strip()[:200])
        return r.returncode == 0

    def send(self, title: str, body: str) -> bool:
        ok = True
        if self.urls:
            ok = self._via_apprise(title, body) and ok
        if self.command:
            ok = self._via_command(title, body) and ok
        return ok


def configured_sender() -> Sender | None:
    urls = config.NOTIFY_URLS
    return Sender(urls, config.NOTIFY_COMMAND) if urls or config.NOTIFY_COMMAND else None


class Notifier:
    """Turns each poll's alerts into messages, and delivers them off the loop."""

    def __init__(self, store, sender: Sender, started: float):
        self.store, self.sender, self.started = store, sender, started
        st = store.get_kv(STATE_KEY) or {}
        # key -> {title, level}: announced and not yet cleared.
        self.open: dict[str, dict] = st.get("open", {})
        # key -> when it was last announced.
        self.last: dict[str, float] = st.get("last", {})
        self.outbox: list[dict] = []
        # Until every alert has had its hold time again, a missing one may
        # simply not be back yet.
        self.grace = max(config.HOLD_SECONDS.values()) + 3 * config.POLL_INTERVAL

    def _save(self):
        self.store.set_kv(STATE_KEY, {"open": self.open, "last": self.last})

    def update(self, alerts: list[dict], now: float) -> None:
        current = {a["key"]: a for a in alerts if a.get("notify")}
        lines = []

        def may_announce(key, level):
            return level == "critical" or now - self.last.get(key, 0) >= config.NOTIFY_COOLDOWN

        for key, a in current.items():
            was = self.open.get(key)
            if was is None:
                # Held back by the cooldown, it stays unannounced - so its clear
                # is not news either - and is announced once the cooldown is
                # over, if it still holds.
                if may_announce(key, a.get("level")):
                    lines.append(line(a))
                    self.open[key] = {"title": a.get("title"), "level": a.get("level")}
                    self.last[key] = now
            elif RANK.get(a.get("level"), 0) > RANK.get(was.get("level"), 0):
                lines.append(line(a, " (escalated)"))
                self.open[key] = {"title": a.get("title"), "level": a.get("level")}
                self.last[key] = now
        if now - self.started >= self.grace:
            for key in list(self.open):
                if key not in current:
                    lines.append(f"🟢 cleared: {self.open.pop(key).get('title', key)}")
        if not lines:
            return
        self._save()
        extra = len(lines) - MAX_LINES
        body = "\n".join(lines[:MAX_LINES]) + (f"\n…and {extra} more" if extra > 0 else "")
        if config.DASHBOARD_URL:
            body += f"\n{config.DASHBOARD_URL}"
        self.outbox.append({"title": config.NOTIFY_TITLE, "body": body, "at": now, "next": now})

    async def deliver(self) -> None:
        """Sends what is due; failed messages wait RETRY_EVERY and try again."""
        now = time.time()
        for msg in list(self.outbox):
            if msg["next"] > now:
                continue
            ok = await asyncio.to_thread(self.sender.send, msg["title"], msg["body"])
            if ok:
                self.outbox.remove(msg)
            elif now - msg["at"] > GIVE_UP_AFTER:
                log.warning("notification dropped after an hour of failures: %s", msg["title"])
                self.outbox.remove(msg)
            else:
                msg["next"] = now + RETRY_EVERY


def main() -> int:
    if "--test" not in sys.argv:
        print(__doc__)
        return 2
    sender = configured_sender()
    if sender is None:
        print("Set NOTIFY_URLS (Apprise URLs) or NOTIFY_COMMAND first.", file=sys.stderr)
        return 2
    title = f"{config.NOTIFY_TITLE}: test"
    body = f"🟢 Test notification from {socket.gethostname()}. Alerts will arrive like this."
    ok = sender.send(title, body)
    print("sent" if ok else "FAILED: see the log lines above")
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
