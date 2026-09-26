"""The runtime adapters: log tails the agent can end, Docker through a fake
Engine API, systemd's unit probe, and how auto picks a runtime."""

import datetime
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from inferenceinquire_agent import runtimes
from inferenceinquire_agent.journal.parser import JournalParser
from inferenceinquire_agent.runtimes import docker, none, proxmox_lxc, systemd, units
from inferenceinquire_agent.runtimes.streams import ProcessStream

FIXTURES = Path(__file__).parent / "fixtures" / "journal"
SLEEPER = [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(60)"]


def read_all(stream, into, done):
    for line in stream:
        into.append(line)
    done.set()


# ------------------------------------------------------------------ streams --


def test_ending_one_tail_leaves_an_identical_one_running():
    """Two agents on one host run the same tail command. Ending one must not
    end the other: the old `pkill -f` on the command line ended both."""
    mine, theirs = ProcessStream(SLEEPER), ProcessStream(SLEEPER)
    try:
        mine.stop()
        assert mine.proc.poll() is not None
        assert theirs.proc.poll() is None
    finally:
        theirs.close()
        mine.close()


def test_stop_reaches_a_writer_the_wrapper_started():
    """lxc-attach's journalctl holds the pipe itself; ending only the wrapper
    never ended the stream. A grandchild stands in for it."""
    wrapper = f"import subprocess, sys, time;subprocess.Popen({SLEEPER!r});time.sleep(60)"
    stream = ProcessStream([sys.executable, "-c", wrapper])
    lines, done = [], threading.Event()
    threading.Thread(target=read_all, args=(stream, lines, done), daemon=True).start()
    deadline = time.time() + 10
    while not lines and time.time() < deadline:
        time.sleep(0.05)
    assert lines == ["up\n"]
    stream.stop()  # from another thread than the reader, like the watchdog
    assert done.wait(8), "the reader never saw EOF"
    stream.close()


# ------------------------------------------------------------------- docker --

LLAMA = {
    "Id": "abc123",
    "Name": "/llama",
    "Path": "/app/llama-server",
    "Args": ["-m", "/models/tiny.gguf", "--host", "0.0.0.0", "--alias", "tiny"],
    "RestartCount": 2,
    "State": {
        "Status": "running",
        "Running": True,
        "Restarting": False,
        "ExitCode": 0,
        "Pid": 4242,
        "StartedAt": "2026-09-25T10:00:00.500000000Z",
    },
    "Config": {"Tty": False, "Env": ["PATH=/usr/bin", "LLAMA_ARG_PORT=8080"]},
    "HostConfig": {"NetworkMode": "bridge"},
    "NetworkSettings": {
        "Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8081"}]},
        "Networks": {"bridge": {"IPAddress": "172.17.0.2"}},
    },
}
OLD = {
    "Id": "def456",
    "Name": "/llama-old",
    "Path": "/app/llama-server",
    "Args": ["--port", "9000"],
    "RestartCount": 0,
    "State": {
        "Status": "exited",
        "Running": False,
        "ExitCode": 1,
        "Pid": 0,
        "StartedAt": "0001-01-01T00:00:00Z",
    },
    "Config": {"Tty": False, "Env": []},
    "HostConfig": {"NetworkMode": "bridge"},
    "NetworkSettings": {"Ports": {}, "Networks": {}},
}
LISTED = [
    {"Id": "abc123", "Names": ["/llama"], "Image": "ghcr.io/ggml-org/llama.cpp:server-cuda",
     "Command": "/app/llama-server -m /models/tiny.gguf"},
    {"Id": "def456", "Names": ["/llama-old"], "Image": "local/llama-old", "Command": ""},
    {"Id": "999", "Names": ["/web"], "Image": "nginx", "Command": "nginx -g daemon off;"},
]  # fmt: skip


def docker_lines():
    """The b10935 fixture, as `docker logs --timestamps` would print it."""
    out = []
    for line in (FIXTURES / "llama-server-b10935.log").read_text().splitlines():
        ts, _, rest = line.partition(" ")
        msg = rest.split(": ", 1)[1]
        stamp = datetime.datetime.fromtimestamp(float(ts), datetime.UTC)
        out.append(stamp.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z " + msg)
    return out


class FakeDocker(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    release = threading.Event()  # ends the log stream when set

    def log_message(self, *args):
        pass

    def _json(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/containers/json":
            return self._json(LISTED)
        for c in (LLAMA, OLD):
            if url.path == f"/containers/{c['Id']}/json":
                return self._json(c)
        if url.path == "/containers/abc123/logs":
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.docker.multiplexed-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for n, line in enumerate(docker_lines()):
                payload = (line + "\n").encode()
                # stdout and stderr alternate, as llama.cpp's do.
                frame = bytes([1 + n % 2, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload
                self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            self.wfile.flush()
            self.release.wait(20)  # a live container: the stream stays open
            return
        return self._json({"message": "no such container"}, 404)


@pytest.fixture
def fake_docker(monkeypatch):
    sock_dir = tempfile.mkdtemp(prefix="dk", dir="/tmp")  # AF_UNIX paths are short
    path = os.path.join(sock_dir, "docker.sock")

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    FakeDocker.release = threading.Event()
    server = Server(path, FakeDocker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DOCKER_HOST", f"unix://{path}")
    yield server
    FakeDocker.release.set()
    server.shutdown()
    server.server_close()
    shutil.rmtree(sock_dir, ignore_errors=True)


def test_docker_finds_the_serving_container(fake_docker):
    ct = docker.collect()["llm_ct"]
    assert set(ct["units"]) == {"llama", "llama-old"}  # not nginx
    llama, old = ct["units"]["llama"], ct["units"]["llama-old"]
    assert (llama["active"], llama["sub"], llama["restarts"]) == ("active", "running", 2)
    assert llama["port"] == 8080  # from LLAMA_ARG_PORT
    assert (llama["model_path"], llama["alias"]) == ("/models/tiny.gguf", "tiny")
    assert llama["active_since"] == pytest.approx(1790330400.5)
    assert (old["active"], old["active_since"], old["port"]) == ("failed", None, 9000)
    assert ct["endpoint"] == "http://127.0.0.1:8081"  # the published port
    assert (ct["active_unit"], ct["vmid"], ct["runtime"], ct["reachable"]) == (
        "llama",
        "docker",
        "docker",
        True,
    )


def test_docker_port_without_a_port_flag():
    """The one port a container exposes, or llama-server's default."""
    info = {
        "Path": "/app/llama-server",
        "Args": ["-m", "/m.gguf"],
        "State": {"Running": True},
        "Config": {"Env": [], "ExposedPorts": {"9931/tcp": {}}},
        "NetworkSettings": {"Ports": {"9931/tcp": [{"HostIp": "", "HostPort": "19931"}]}},
    }
    unit, url = docker.unit_from(info)
    assert (unit["port"], url) == (9931, "http://127.0.0.1:19931")
    info["Config"]["ExposedPorts"] = {"8080/tcp": {}, "9000/tcp": {}}
    info["NetworkSettings"]["Ports"] = {}
    assert docker.unit_from(info)[0]["port"] == docker.LLAMA_DEFAULT_PORT


def test_docker_through_a_tcp_proxy(monkeypatch):
    """DOCKER_HOST=tcp://... for a socket proxy that only allows GETs."""
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDocker)
    FakeDocker.release = threading.Event()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DOCKER_HOST", f"tcp://127.0.0.1:{server.server_address[1]}")
    try:
        assert docker.available()
        ct = docker.collect()["llm_ct"]
        assert (ct["active_unit"], ct["endpoint"]) == ("llama", "http://127.0.0.1:8081")
    finally:
        FakeDocker.release.set()
        server.shutdown()
        server.server_close()


def test_docker_without_a_daemon_reports_unreachable(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    ct = docker.collect()["llm_ct"]
    assert (ct["reachable"], ct["vmid"], ct["units"]) == (False, None, {})


def test_docker_logs_parse_like_the_journal(fake_docker):
    """The same build's log through Docker's stream gives the same requests,
    counters and matches as through journalctl."""
    docker.collect()
    stream = docker.follow("docker", "llama")
    lines, done = [], threading.Event()
    threading.Thread(target=read_all, args=(stream, lines, done), daemon=True).start()
    want_n = len(docker_lines())
    deadline = time.time() + 10
    while len(lines) < want_n and time.time() < deadline:
        time.sleep(0.05)
    assert len(lines) == want_n

    journal = (FIXTURES / "llama-server-b10935.log").read_text().splitlines()
    assert lines[0].split(" ", 1)[0] == journal[0].split(" ", 1)[0]  # the time survives
    via_docker, via_journal = JournalParser(), JournalParser()
    for line in lines:
        via_docker.ingest(line.rstrip("\n"))
    for line in journal:
        via_journal.ingest(line)
    a, b = via_docker.snapshot(), via_journal.snapshot()
    for key in ("matched", "counters", "requests"):
        assert a[key] == b[key], key

    stream.stop()  # the container still runs: only our socket ends
    assert done.wait(8), "stop() did not end a blocked read"
    stream.close()


def test_docker_reads_the_model_through_the_containers_root(monkeypatch, tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "tiny.gguf").write_bytes(b"GGUF" + bytes(60))
    monkeypatch.setitem(docker._seen, "llama", {"id": "abc123", "pid": 4242, "tty": False})
    real_open = open

    def fake_open(path, *a, **kw):
        prefix = "/proc/4242/root"
        if str(path).startswith(prefix):
            path = str(tmp_path) + str(path)[len(prefix) :]
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert docker.read_head("docker", "llama", "/models/tiny.gguf", 8) == b"GGUF" + bytes(4)
    assert docker.read_head("docker", "missing", "/models/tiny.gguf", 8) is None


# ------------------------------------------------------------------ systemd --

PROBE_OUT = """\
UNIT:llama-server:ActiveState=active|SubState=running|NRestarts=1|ExecMainStartTimestampMonotonic=5|MemoryCurrent=1048576|
EXEC:llama-server:{ path=/opt/llama/llama-server ; argv[]=/opt/llama/llama-server --model /m/a.gguf --port 8080 --host 127.0.0.1 --cache-ram 24576 ; }
SINCE:llama-server:1790000000
UNIT:llama-other:ActiveState=inactive|SubState=dead|NRestarts=0|ExecMainStartTimestampMonotonic=0|MemoryCurrent=[not set]|
EXEC:llama-other:{ argv[]=/opt/llama/llama-server -m /m/b.gguf --port 8080 ; }
SINCE:llama-other:
"""  # noqa: E501


def test_systemd_units_on_this_host(monkeypatch):
    calls = []
    monkeypatch.setattr(systemd, "_discovery", {"at": 0.0, "units": []})
    monkeypatch.setattr(systemd, "list_units", lambda prefix: ["llama-server", "llama-other"])
    monkeypatch.setattr(units, "run", lambda argv, timeout: calls.append(argv) or PROBE_OUT)
    ct = systemd.collect()["llm_ct"]
    assert calls[0][:2] == ["bash", "-c"]  # on this host: no container prefix
    assert "UPTIME" not in calls[0][2]  # the host's own stats come from the collectors
    a, b = ct["units"]["llama-server"], ct["units"]["llama-other"]
    assert (a["active"], a["restarts"], a["memory"], a["active_since"]) == (
        "active",
        1,
        1048576,
        1790000000,
    )
    assert (a["model_path"], a["cache_ram_mib"], a["cache_ram_set"]) == ("/m/a.gguf", 24576, True)
    assert (b["active"], b["memory"], b["active_since"], b["model_path"]) == (
        "inactive",
        None,
        None,
        "/m/b.gguf",
    )
    assert (ct["active_unit"], ct["endpoint"], ct["vmid"], ct["label"]) == (
        "llama-server",
        "http://127.0.0.1:8080",
        "host",
        "this host",
    )


def test_systemd_with_no_llama_units(monkeypatch):
    monkeypatch.setattr(systemd, "_discovery", {"at": 0.0, "units": []})
    monkeypatch.setattr(systemd, "list_units", lambda prefix: [])
    ct = systemd.collect()["llm_ct"]
    assert (ct["vmid"], ct["units"], ct["runtime"]) == (None, {}, "systemd")


# ------------------------------------------------------------ auto-selection --


def fake_runtime(name, active):
    class R:
        NAME = name

        @staticmethod
        def collect():
            u = {"x": {"active": "active" if active else "inactive"}}
            ct = {"vmid": name, "units": u, "runtime": name}
            if active:
                ct["active_unit"] = "x"
            return {"guests": [], "llm_ct": ct}

        @staticmethod
        def follow(target, unit):
            return f"{name}:{unit}"

    return R


def test_auto_prefers_proxmox_then_whatever_is_serving(monkeypatch):
    monkeypatch.setattr(proxmox_lxc, "available", lambda: True)
    assert runtimes.candidates("auto") == [proxmox_lxc]
    monkeypatch.setattr(proxmox_lxc, "available", lambda: False)
    monkeypatch.setattr(docker, "available", lambda: True)
    monkeypatch.setattr(systemd, "available", lambda: True)
    assert runtimes.candidates("auto") == [docker, systemd]
    monkeypatch.setattr(docker, "available", lambda: False)
    monkeypatch.setattr(systemd, "available", lambda: False)
    assert runtimes.candidates("auto") == [none]
    assert runtimes.candidates("docker") == [docker]
    with pytest.raises(SystemExit):
        runtimes.candidates("kubernetes")

    idle, serving = fake_runtime("idle", False), fake_runtime("serving", True)
    monkeypatch.setattr(runtimes, "CANDIDATES", [idle, serving])
    assert runtimes.collect_guests()["llm_ct"]["runtime"] == "serving"
    assert runtimes.follow("serving", "x") == "serving:x"  # the log comes from its owner


# -------------------------------------------------------------- allowlist --


def test_the_allowlist_admits_only_listed_addresses_and_this_machine():
    from inferenceinquire_agent.server import Handler, Server

    server = Server(("127.0.0.1", 0), Handler, ["10.0.0.21", "192.168.5.0/24"])
    try:
        ok = lambda ip: server.verify_request(None, (ip, 40000))  # noqa: E731
        assert ok("10.0.0.21") and ok("192.168.5.77")
        assert ok("127.0.0.1") and ok("::1") and ok("::ffff:10.0.0.21")
        assert not ok("10.0.0.22") and not ok("192.168.6.1") and not ok("garbage")
    finally:
        server.server_close()
    open_server = Server(("127.0.0.1", 0), Handler)
    try:
        assert open_server.verify_request(None, ("203.0.113.9", 1))
    finally:
        open_server.server_close()
