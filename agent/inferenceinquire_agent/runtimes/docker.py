"""Docker: llama-server in a container on this host, found through the Docker
Engine API on its unix socket.

Stdlib only, and read-only: every request is a GET (list, inspect, logs), so
the socket can sit behind a proxy that allows nothing else (DOCKER_HOST can
be tcp://host:port for one). The model file is
read through /proc/<pid>/root, the container's own view of its filesystem,
rather than by running anything inside it; that needs the agent in the host's
pid namespace (the host itself, or `pid: host`).
"""

import calendar
import fnmatch
import http.client
import json
import os
import re
import socket
import time
import urllib.parse

from .. import config
from .llama_args import args_with_env, loopback_url, settings_from_args

NAME = "docker"
TARGET = "docker"
LLAMA_DEFAULT_PORT = 8080


def docker_host():
    """("unix", path) or ("tcp", host, port), from DOCKER_HOST."""
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("tcp://"):
        name, _, port = host[len("tcp://") :].rstrip("/").rpartition(":")
        return ("tcp", name, int(port)) if name else ("tcp", port, 2375)
    if host.startswith("unix://"):
        return ("unix", host[len("unix://") :])
    return ("unix", "/var/run/docker.sock")


def available():
    where = docker_host()
    return where[0] == "tcp" or os.path.exists(where[1])


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.socket_path)
        self.sock = s


def connection(timeout):
    where = docker_host()
    if where[0] == "tcp":
        return http.client.HTTPConnection(where[1], where[2], timeout=timeout)
    return UnixConnection(where[1], timeout)


def get(path, timeout=5.0):
    """A GET on the Engine API, as parsed JSON; None when it did not answer."""
    conn = connection(timeout)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        return json.loads(body) if r.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def is_llama(c):
    """A container that runs llama-server: by name, image or command."""
    name = (c.get("Names") or ["/"])[0].lstrip("/")
    image = (c.get("Image") or "").lower()
    return (
        fnmatch.fnmatch(name, config.UNIT_GLOB)
        or any(s in image for s in ("llama.cpp", "llama-server", "llamacpp"))
        or "llama-server" in (c.get("Command") or "")
    )


STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z")


def epoch(stamp):
    """Docker's RFC 3339 times (nanoseconds, Z) as epoch seconds; None for unset."""
    m = STAMP.match(stamp or "")
    if not m or m.group(1).startswith("0001"):
        return None
    whole = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
    return whole + float("0" + (m.group(2) or ""))


def unit_state(st):
    """A container's state in systemd's words, which the dashboard reads."""
    if st.get("Restarting"):
        return "activating"
    if st.get("Running"):
        return "active"
    return "failed" if st.get("ExitCode") else "inactive"


def endpoint(info, port, bind):
    """Where the server answers from this host's network."""
    if (info.get("HostConfig") or {}).get("NetworkMode") == "host":
        return loopback_url(bind, port)
    net = info.get("NetworkSettings") or {}
    published = (net.get("Ports") or {}).get(f"{port}/tcp") or []
    if published:
        return loopback_url(published[0].get("HostIp"), published[0].get("HostPort"))
    for n in (net.get("Networks") or {}).values():
        if n.get("IPAddress"):
            return f"http://{n['IPAddress']}:{port}"
    return None


def unit_from(info):
    """(unit, endpoint) for one inspected container."""
    st = info.get("State") or {}
    cfg = info.get("Config") or {}
    env = dict(e.split("=", 1) for e in (cfg.get("Env") or []) if "=" in e)
    unit = {
        "active": unit_state(st),
        "sub": st.get("Status"),
        "restarts": info.get("RestartCount", 0),
        "memory": None,
        "active_since": epoch(st.get("StartedAt")) if st.get("Running") else None,
        **settings_from_args(
            args_with_env([info.get("Path") or "", *(info.get("Args") or [])], env)
        ),
    }
    if unit["port"] is None:
        # No --port: the one TCP port the container publishes or exposes, else
        # llama-server's own default (which upstream means to change).
        net_ports = (info.get("NetworkSettings") or {}).get("Ports") or {}
        tcp = {k for k in (*net_ports, *(cfg.get("ExposedPorts") or {})) if k.endswith("/tcp")}
        unit["port"] = int(tcp.pop().split("/")[0]) if len(tcp) == 1 else LLAMA_DEFAULT_PORT
    url = endpoint(info, unit["port"], unit["bind"]) if st.get("Running") else None
    return unit, url


# What follow() and read_head() need and the dashboard does not, by unit name.
_seen = {}


def collect():
    target = {
        "vmid": None,
        "reachable": False,
        "units": {},
        "discovered": True,
        "runtime": NAME,
        "label": None,
    }
    listed = get("/containers/json?all=1")
    if listed is None:
        return {"guests": [], "llm_ct": target}
    target["reachable"] = True
    urls = {}
    for c in sorted(filter(is_llama, listed), key=lambda c: c.get("Names") or []):
        info = get(f"/containers/{urllib.parse.quote(c['Id'])}/json")
        if not info:
            continue
        name = (info.get("Name") or c["Id"][:12]).lstrip("/")
        target["units"][name], urls[name] = unit_from(info)
        _seen[name] = {
            "id": info.get("Id") or c["Id"],
            "pid": (info.get("State") or {}).get("Pid") or None,
            "tty": bool((info.get("Config") or {}).get("Tty")),
        }
    if target["units"]:
        target.update(vmid=TARGET, label="Docker")
    for name, u in target["units"].items():
        if u["active"] == "active" and urls.get(name):
            target["endpoint"], target["active_unit"] = urls[name], name
            break
    return {"guests": [], "llm_ct": target}


class LogStream:
    """`docker logs -f`, straight from the Engine API.

    Lines come out in journalctl's short-unix shape (epoch, source, message),
    so the one parser reads every runtime. Without a TTY the API multiplexes
    stdout and stderr in frames with an 8-byte header; with one it is raw.
    stop() shuts the socket down, which ends a blocked read in another thread.
    """

    def __init__(self, name, cid, tty):
        self.name, self.tty = name, tty
        self.conn = connection(None)
        query = "follow=1&stdout=1&stderr=1&timestamps=1&tail=40"
        self.conn.request("GET", f"/containers/{urllib.parse.quote(cid)}/logs?{query}")
        self.resp = self.conn.getresponse()
        if self.resp.status != 200:
            self.close()
            raise OSError(f"docker logs: HTTP {self.resp.status}")

    def _read(self, n):
        out = b""
        while len(out) < n:
            chunk = self.resp.read(n - len(out))
            if not chunk:
                return None
            out += chunk
        return out

    def _chunks(self):
        while True:
            if self.tty:
                chunk = self.resp.read1(65536)
            else:
                head = self._read(8)
                chunk = head and self._read(int.from_bytes(head[4:8], "big"))
            if not chunk:
                return
            yield chunk

    def shape(self, line):
        m = STAMP.match(line)
        ts = (epoch(line) if m else None) or time.time()
        msg = line[m.end() :].lstrip(" ") if m else line
        return f"{ts:.6f} {self.name} llama-server[docker]: {msg}"

    def __iter__(self):
        buf = b""
        try:
            for chunk in self._chunks():
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    yield self.shape(raw.decode("utf-8", "replace").rstrip("\r")) + "\n"
        except (OSError, ValueError, http.client.HTTPException):
            return

    def stop(self):
        try:
            if self.conn.sock:
                self.conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self):
        self.stop()
        try:
            self.resp.close()
        except Exception:
            pass
        self.conn.close()


def follow(target, unit):
    seen = _seen.get(unit)
    if not seen:
        return None
    return LogStream(unit, seen["id"], seen["tty"])


def read_head(target, unit, path, size):
    pid = (_seen.get(unit) or {}).get("pid")
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/root{path}", "rb") as f:
            return f.read(size)
    except OSError:
        return None
