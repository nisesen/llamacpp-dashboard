"""What happens to alerts after the rules: hold times, model-switch
transitions, the push policy and severity order.
"""

from __future__ import annotations

from pathlib import Path

from . import config


def should_notify(key: str, level: str) -> bool:
    if key in config.PUSH_SKIP_KEYS:
        return False
    if level in ("critical", "serious"):
        return True
    return level == "warning" and key.startswith(config.NOTIFY_WARNING_PREFIXES)


def transition_signal(snap: dict) -> str | None:
    """Why the server is visibly mid-restart or mid-switch, or None."""
    if (snap.get("llama") or {}).get("loading"):
        return "llama.cpp is loading the model"
    load = (snap.get("journal") or {}).get("load") or {}
    if load.get("state") == "loading":
        return f"loading {Path(load.get('path') or '').name or 'model'}"
    for name, u in sorted((snap.get("units") or {}).items()):
        if u.get("active") in ("activating", "deactivating", "reloading"):
            return f"{name} {u['active']}"
    return None


class AlertGate:
    """Turns the rules' raw output into what is shown, logged and pushed.

    evaluate_alerts() says what is true on this poll. This decides what is
    worth saying: a condition must hold for its HOLD_SECONDS, and while a
    restart or model switch is visibly under way the reachability rules it
    trips by design are held back. A planned switch used to log three or four
    criticals ("unreachable", "unhealthy", "no unit running") every time.
    """

    def __init__(self):
        self.first_seen: dict[str, float] = {}
        self.transition: dict | None = None

    def update_transition(self, reason: str | None, now: float):
        """Track the transition; returns ("started"|"ended", info) on an edge."""
        if reason:
            if self.transition is None:
                self.transition = {"since": now, "reason": reason, "last": now}
                return "started", dict(self.transition)
            self.transition.update(reason=reason, last=now)
        elif self.transition and now - self.transition["last"] > config.TRANSITION_TAIL:
            info = dict(self.transition, ended=now)
            self.transition = None
            return "ended", info
        return None

    def suppressing(self, now: float) -> bool:
        return (
            self.transition is not None and now - self.transition["since"] < config.TRANSITION_MAX
        )

    def view(self, now: float) -> dict | None:
        if not self.transition:
            return None
        return dict(
            self.transition,
            elapsed=now - self.transition["since"],
            stalled=not self.suppressing(now),
        )

    def apply(self, alerts: list[dict], now: float) -> list[dict]:
        present = {a["key"] for a in alerts}
        for k in list(self.first_seen):
            if k not in present:
                del self.first_seen[k]
        # First-seen is tracked even while suppressed, so a load that ends in
        # failure alerts the moment the transition closes, not a hold later.
        suppress = self.suppressing(now)
        out = []
        for a in alerts:
            k = a["key"]
            since = self.first_seen.setdefault(k, now)
            if suppress and k in config.TRANSITION_KEYS:
                continue
            if now - since < config.HOLD_SECONDS.get(k, 0):
                continue
            out.append(dict(a, since=since, notify=should_notify(k, a["level"])))
        return out


LEVEL_ORDER = {"critical": 3, "serious": 2, "warning": 1}


def worst_level(alerts: list[dict]) -> str:
    return max(
        (a["level"] for a in alerts), key=lambda lvl: LEVEL_ORDER.get(lvl, 0), default="good"
    )
