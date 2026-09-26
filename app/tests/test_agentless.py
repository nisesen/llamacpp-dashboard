"""Running with or without the host agent: what is polled, which endpoint is
used, the token, and the alerts that depend on the agent."""

import asyncio
import os
import stat

import httpx
import pytest
from inferenceinquire import config, sources
from inferenceinquire.monitor import Monitor
from support import evaluate, healthy_snap, make_store

AGENT = "http://agent.test:9101"
FOUND = "http://10.0.0.5:8080"  # where the agent says llama.cpp answers


def agent_payload():
    return {
        "agent_ok": True,
        "guests": {
            "guests": [],
            "llm_ct": {
                "vmid": "docker",
                "runtime": "docker",
                "label": "Docker",
                "reachable": True,
                "units": {"llama": {"active": "active", "port": 8080}},
                "active_unit": "llama",
                "endpoint": FOUND,
            },
        },
    }


def poll(tmp_path, monkeypatch, *, agent_url, llama_url):
    """One poll against a fake network; returns (requested URLs, snapshot)."""
    monkeypatch.setattr(config, "AGENT_URL", agent_url)
    monkeypatch.setattr(config, "AGENT_ENABLED", bool(agent_url))
    monkeypatch.setattr(config, "AGENT_TOKEN", "t")
    monkeypatch.setattr(config, "LLAMA_URL", llama_url)
    seen = []

    def answer(request):
        seen.append(str(request.url))
        if request.url.path == "/metrics.json":
            return httpx.Response(200, json=agent_payload())
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    m = Monitor(make_store(tmp_path))

    async def run():
        m.client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        await m.poll_once()
        await m.client.aclose()

    asyncio.run(run())
    return seen, m.snapshot


def test_without_an_agent_nothing_asks_for_one(tmp_path, monkeypatch):
    seen, snap = poll(tmp_path, monkeypatch, agent_url="", llama_url="http://laptop:8080")
    assert not [u for u in seen if "metrics.json" in u]
    assert seen[0] == "http://laptop:8080/health"
    assert snap["agent_enabled"] is False
    assert snap["llama"]["healthy"] is True
    assert "agent" not in {a["key"] for a in snap["alerts"]}


def test_with_an_agent_the_snapshot_carries_no_flag(tmp_path, monkeypatch):
    """The flag appears only when the agent is off, so a deployment with an
    agent sends exactly what it sent before."""
    _, snap = poll(tmp_path, monkeypatch, agent_url=AGENT, llama_url="")
    assert "agent_enabled" not in snap


def test_the_discovered_endpoint_is_followed_when_llama_url_is_unset(tmp_path, monkeypatch):
    seen, _ = poll(tmp_path, monkeypatch, agent_url=AGENT, llama_url="")
    assert f"{FOUND}/health" in seen


def test_llama_url_wins_over_the_discovered_endpoint(tmp_path, monkeypatch):
    seen, _ = poll(tmp_path, monkeypatch, agent_url=AGENT, llama_url="http://127.0.0.1:8080")
    assert "http://127.0.0.1:8080/health" in seen
    assert not [u for u in seen if u.startswith(FOUND)]


# -------------------------------------------------------------------- token --


def test_the_token_file_is_made_once_and_kept(tmp_path, monkeypatch):
    path = tmp_path / "agent-token"
    monkeypatch.setattr(config, "AGENT_TOKEN", "")
    monkeypatch.setattr(config, "AGENT_TOKEN_FILE", str(path))
    monkeypatch.setattr(sources, "_token", {})
    first = sources.agent_token()
    assert len(first) == 48 and path.read_text().strip() == first
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    monkeypatch.setattr(sources, "_token", {})  # a restart reads, not remakes
    assert sources.agent_token() == first


def test_agent_token_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TOKEN", "given")
    monkeypatch.setattr(config, "AGENT_TOKEN_FILE", str(tmp_path / "unused"))
    assert sources.agent_token() == "given"
    assert not (tmp_path / "unused").exists()


# ------------------------------------------------------------------- alerts --


@pytest.mark.parametrize("enabled,raised", [(True, True), (False, False)])
def test_agent_alert_only_when_an_agent_is_configured(tmp_path, monkeypatch, enabled, raised):
    monkeypatch.setattr(config, "AGENT_ENABLED", enabled)
    snap = dict(healthy_snap(), agent_ok=False)
    assert ("agent" in evaluate(make_store(tmp_path), snap, {})) is raised


def test_runtime_none_expects_no_inference_server(tmp_path):
    snap = dict(healthy_snap(), llm_ct={"vmid": None, "runtime": "none", "units": {}})
    assert not {"ct", "ct_missing"} & evaluate(make_store(tmp_path), snap, {})


def test_target_alerts_name_the_runtime(tmp_path):
    from inferenceinquire.rules import inference_container

    def titles(ct):
        snap = dict(healthy_snap(), llm_ct=ct)
        return [a["title"] for a in inference_container(snap, {}, make_store(tmp_path))]

    # An agent from before the runtimes: the Proxmox wording, as it always was.
    assert titles({"vmid": "100", "reachable": False}) == ["Cannot probe CT 100"]
    assert titles({"vmid": None}) == ["No inference container found"]
    assert titles({"vmid": "docker", "runtime": "docker", "reachable": False}) == [
        "Cannot read Docker"
    ]
    assert titles({"vmid": None, "runtime": "systemd"}) == ["No llama.cpp unit found"]
