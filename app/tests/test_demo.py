"""Demo mode: the recorded session replays as if live, and loops seamlessly."""

import asyncio
import json
import time

import httpx
import pytest
from inferenceinquire.demo import ReplayTransport, Session, seed
from inferenceinquire.sources import parse_prometheus
from support import make_store


@pytest.fixture(scope="module")
def session():
    return Session()


def agent_at(session, k, i):
    _, body = session.answer(k, i, "/metrics.json")
    return json.loads(body)


def test_the_recording_plays_as_now(session):
    a = agent_at(session, 0, 0)
    assert abs(a["ts"] - session.started) < session.poll_interval
    finished = [r["finished"] for r in a["journal"]["requests"] if r.get("finished")]
    assert finished and max(finished) <= a["ts"]


def test_the_loop_moves_time_on_and_counters_never_go_back(session):
    last = agent_at(session, 0, len(session.frames) - 1)
    again = agent_at(session, 1, 0)
    assert again["ts"] > last["ts"]
    assert again["ts"] - last["ts"] < 3 * session.poll_interval
    assert again["cpu"]["total"][0] >= last["cpu"]["total"][0]
    for name, iface in again["net"]["ifaces"].items():
        assert iface["rx"] >= last["net"]["ifaces"][name]["rx"]

    def tokens(k, i):
        _, body = session.answer(k, i, "/metrics")
        return parse_prometheus(body)[0]["llamacpp:tokens_predicted_total"]

    assert tokens(1, 0) >= tokens(0, len(session.frames) - 1)


def test_the_bmc_log_keeps_its_own_dates(session):
    a, b = agent_at(session, 0, 0), agent_at(session, 3, 0)
    assert [e.get("ts") for e in a["sel"]["entries"]] == [e.get("ts") for e in b["sel"]["entries"]]


def test_every_poll_is_answered_from_one_moment(session):
    async def run():
        async with httpx.AsyncClient(transport=ReplayTransport(session)) as c:
            agent = (await c.get("http://agent:9101/metrics.json")).json()
            props = (await c.get("http://llama:8080/props")).json()
            missing = await c.get("http://llama:8080/nope")
            return agent, props, missing.status_code

    agent, props, missing = asyncio.run(run())
    assert agent["agent_ok"] and props["model_alias"]
    assert missing == 404


def test_seed_fills_a_week_ending_now(tmp_path):
    st = make_store(tmp_path)
    now = time.time()
    seed(st, now)
    lo, hi = st.conn.execute("SELECT MIN(ts), MAX(ts) FROM samples").fetchone()
    assert now - hi < 10 and 6.5 * 86400 < hi - lo < 7.5 * 86400
    assert st.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] > 100
    n = st.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    seed(st, now + 60)  # a store that has history is left alone
    assert st.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == n
