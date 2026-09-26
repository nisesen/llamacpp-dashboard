# An external alert forwarder

The dashboard can send alerts itself (`NOTIFY_URLS`), but it cannot tell you
that it has stopped: a dashboard that is down sends nothing. This forwarder
runs on another machine, from a systemd timer every minute, and polls the
dashboard's `/api/alerts`. It forwards raised, escalated and cleared alerts,
and says so when the dashboard stops answering or stops polling. Stdlib only.

`DASH_SEND` is any command that sends one message; the text arrives on stdin
and in `$DASH_MESSAGE`.

```bash
install -D dash-alerts.py ~/.local/bin/dash-alerts.py
cp dash-alerts.service dash-alerts.timer ~/.config/systemd/user/
cat > ~/.config/dash-alerts.env <<'EOT'
DASH_URL=http://<dashboard>:8000
DASH_SEND=apprise -b "$DASH_MESSAGE" tgram://BOT_TOKEN/CHAT_ID
EOT
systemctl --user daemon-reload && systemctl --user enable --now dash-alerts.timer
DRY_RUN=1 DASH_URL=http://<dashboard>:8000 DASH_STATE=/tmp/x.json python3 dash-alerts.py   # prints, sends nothing
```

Use either this or `NOTIFY_URLS` for the alerts themselves, not both, or each
arrives twice. Keeping this one just for "the dashboard is down" is fine: set
the dashboard's `PUSH_SKIP_KEYS` so nothing else overlaps.
