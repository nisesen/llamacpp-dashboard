"""The HTTP endpoint: /metrics.json behind a bearer token, /healthz open."""

import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .payload import build_payload


def load_token():
    try:
        with open(config.TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return None


def allowed_networks(entries):
    return [ipaddress.ip_network(e, strict=False) for e in entries]


class Server(ThreadingHTTPServer):
    """Drops a connection from an address AGENT_ALLOW does not list, before
    a byte of HTTP is read. This machine is always let in."""

    def __init__(self, address, handler, allow=()):
        self.allow = allowed_networks(allow)
        super().__init__(address, handler)

    def verify_request(self, request, client_address):
        if not self.allow:
            return True
        try:
            ip = ipaddress.ip_address(client_address[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return ip.is_loopback or any(ip in net for net in self.allow)


class Handler(BaseHTTPRequestHandler):
    server_version = f"inferenceinquire-agent/{config.AGENT_VERSION}"

    def log_message(self, *args):
        pass  # journald noise

    def _send(self, code, body, ctype="application/json"):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._send(200, "ok\n", "text/plain")
        if path != "/metrics.json":
            return self._send(404, json.dumps({"error": "not found"}))

        token = load_token()
        header = self.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.startswith("Bearer ") else ""
        if not token or supplied != token:
            return self._send(401, json.dumps({"error": "unauthorized"}))
        try:
            return self._send(200, json.dumps(build_payload()))
        except Exception as exc:
            return self._send(500, json.dumps({"error": str(exc)}))
