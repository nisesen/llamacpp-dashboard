"""One version for the release: the dashboard, the agent and the changelog."""

import re
from pathlib import Path

import pytest
from inferenceinquire import __version__

ROOT = Path(__file__).resolve().parent.parent.parent


def repo_file(rel):
    path = ROOT / rel
    if not path.exists():
        pytest.skip("not in a checkout (the image carries app/ only)")
    return path.read_text()


def test_the_agent_carries_the_same_version():
    agent = repo_file("agent/inferenceinquire_agent/config.py")
    assert re.search(r'^AGENT_VERSION = "([^"]+)"', agent, re.M).group(1) == __version__


def test_the_changelog_describes_this_version():
    first = re.search(r"^## \[?(\d[^\]\s]*)", repo_file("CHANGELOG.md"), re.M).group(1)
    assert first == __version__
