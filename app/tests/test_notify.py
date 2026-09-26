"""Push notifications: what is announced, when, and delivery."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from inferenceinquire import config
from inferenceinquire.notify import Notifier, Sender
from support import make_store

T0 = 1_000_000.0
GRACE = max(config.HOLD_SECONDS.values()) + 3 * config.POLL_INTERVAL


def alert(key, level="serious", title=None, notify=True):
    return {"key": key, "level": level, "title": title or key, "detail": "", "notify": notify}


class Recorder:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def send(self, title, body):
        self.sent.append((title, body))
        return self.ok


def notifier(tmp_path, store=None):
    return Notifier(store or make_store(tmp_path), Recorder(), started=T0)


def bodies(n):
    return [m["body"] for m in n.outbox]


def test_a_raise_is_sent_once_and_its_clear_once(tmp_path):
    n = notifier(tmp_path)
    n.update([alert("gpu_count", "critical", "A GPU has disappeared")], T0 + GRACE)
    n.update([alert("gpu_count", "critical", "A GPU has disappeared")], T0 + GRACE + 2)
    n.update([], T0 + GRACE + 4)
    n.update([], T0 + GRACE + 6)
    assert bodies(n) == ["🔴 A GPU has disappeared", "🟢 cleared: A GPU has disappeared"]


def test_only_push_worthy_alerts(tmp_path):
    n = notifier(tmp_path)
    n.update([alert("llama", notify=False)], T0 + GRACE)
    assert n.outbox == []


def test_nothing_is_called_cleared_before_alerts_had_their_hold_again(tmp_path):
    """After a restart the open set says an alert was on; it reappears only
    once its hold time has passed again, and must not be 'cleared' before."""
    store = make_store(tmp_path)
    first = Notifier(store, Recorder(), started=T0)
    first.update([alert("pwr:0", "warning", "GPU 0 over its cap")], T0 + GRACE)
    restarted = Notifier(store, Recorder(), started=T0 + 1000)
    restarted.update([], T0 + 1002)  # held, not gone
    restarted.update([alert("pwr:0", "warning", "GPU 0 over its cap")], T0 + 1020)
    assert restarted.outbox == []  # already announced before the restart
    restarted.update([], T0 + 1000 + GRACE + 1)
    assert bodies(restarted) == ["🟢 cleared: GPU 0 over its cap"]


def test_a_flapping_warning_is_announced_at_most_once_per_cooldown(tmp_path):
    n = notifier(tmp_path)
    t = T0 + GRACE
    n.update([alert("hbm:0", "warning", "HBM hot")], t)
    n.update([], t + 60)
    n.update([alert("hbm:0", "warning", "HBM hot")], t + 120)  # back within the cooldown
    n.update([], t + 180)  # its clear is not news either
    assert bodies(n) == ["🟡 HBM hot", "🟢 cleared: HBM hot"]
    n.update([alert("hbm:0", "warning", "HBM hot")], t + config.NOTIFY_COOLDOWN + 1)
    assert bodies(n)[-1] == "🟡 HBM hot"


def test_a_held_back_raise_is_sent_when_the_cooldown_ends_if_it_still_holds(tmp_path):
    n = notifier(tmp_path)
    t = T0 + GRACE
    n.update([alert("hbm:0", "warning", "HBM hot")], t)
    n.update([], t + 60)
    for dt in range(120, int(config.NOTIFY_COOLDOWN) + 60, 30):
        n.update([alert("hbm:0", "warning", "HBM hot")], t + dt)
    assert bodies(n) == ["🟡 HBM hot", "🟢 cleared: HBM hot", "🟡 HBM hot"]


def test_criticals_are_always_announced(tmp_path):
    n = notifier(tmp_path)
    t = T0 + GRACE
    for i in range(3):
        n.update([alert("llama", "critical", "Inference server unreachable")], t + 60 * i)
        n.update([], t + 60 * i + 30)
    assert len([b for b in bodies(n) if b.startswith("🔴")]) == 3


def test_an_escalation_is_news(tmp_path):
    n = notifier(tmp_path)
    t = T0 + GRACE
    n.update([alert("pwr:1", "warning", "GPU 1 over its cap")], t)
    n.update([alert("pwr:1", "serious", "GPU 1 well over its cap")], t + 20)
    assert bodies(n)[-1] == "🟠 GPU 1 well over its cap (escalated)"


def test_many_alerts_make_one_message(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_URL", "http://dash.example")
    n = notifier(tmp_path)
    n.update([alert(f"disk:{i}", "serious", f"disk {i}") for i in range(15)], T0 + GRACE)
    (msg,) = n.outbox
    assert msg["title"] == config.NOTIFY_TITLE
    assert msg["body"].count("\n🟠") == 11 and "…and 3 more" in msg["body"]
    assert msg["body"].endswith("\nhttp://dash.example")


# ----------------------------------------------------------------- delivery --


def test_a_failed_delivery_is_retried_then_dropped(tmp_path, monkeypatch):
    n = notifier(tmp_path)
    n.sender = Recorder(ok=False)
    n.update([alert("gpu_count", "critical")], T0 + GRACE)
    clock = [T0 + GRACE]
    monkeypatch.setattr("inferenceinquire.notify.time.time", lambda: clock[0])
    asyncio.run(n.deliver())
    assert len(n.sender.sent) == 1 and len(n.outbox) == 1
    asyncio.run(n.deliver())  # not due yet
    assert len(n.sender.sent) == 1
    clock[0] += 61
    n.sender.ok = True
    asyncio.run(n.deliver())
    assert len(n.sender.sent) == 2 and n.outbox == []

    n.sender.ok = False
    n.update([], T0 + GRACE + 3000)
    clock[0] = T0 + GRACE + 3000 + 3700
    asyncio.run(n.deliver())
    assert n.outbox == []  # given up after an hour


def test_the_command_gets_the_message_on_stdin(tmp_path):
    out = tmp_path / "msg.txt"
    s = Sender([], f'printf "%s\\n" "$NOTIFY_TITLE" > {out}; cat >> {out}')
    assert s.send("InferenceInquire", "🔴 A GPU has disappeared")
    assert out.read_text() == "InferenceInquire\n🔴 A GPU has disappeared"
    assert not Sender([], "exit 3").send("t", "b")


@pytest.fixture
def webhook():
    """A local JSON webhook, which Apprise reaches as json://."""
    got = []

    class Hook(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"json://127.0.0.1:{server.server_address[1]}/hook", got
    server.shutdown()


def test_apprise_delivers_to_a_webhook(webhook):
    url, got = webhook
    assert Sender([url], "").send("InferenceInquire", "🔴 A GPU has disappeared")
    assert [(g["title"], g["message"]) for g in got] == [
        ("InferenceInquire", "🔴 A GPU has disappeared")
    ]


def test_the_test_command(webhook):
    url, got = webhook
    app = Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [sys.executable, "-m", "inferenceinquire.notify", "--test"],
        cwd=app,
        env={**os.environ, "NOTIFY_URLS": url, "DB_PATH": ":memory:"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert got and got[0]["title"] == "InferenceInquire: test"
