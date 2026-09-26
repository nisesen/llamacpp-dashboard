"""The agent's self-test checks, one test each: the same checks the installer
runs on the host before it replaces a working agent."""

import subprocess
import sys
from pathlib import Path

import pytest
from inferenceinquire_agent.selftest import CHECKS, self_test


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_check(check):
    assert check()


def test_self_test_reports_success():
    assert self_test() == 0


def test_the_built_file_runs_its_self_test(tmp_path):
    """What the installer ships: one file, stdlib only."""
    agent = Path(__file__).resolve().parent.parent
    out = tmp_path / "inferenceinquire-agent.pyz"
    subprocess.run(["bash", str(agent / "build.sh"), str(out)], check=True, capture_output=True)
    run = subprocess.run([sys.executable, str(out), "--self-test"], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "SELF-TEST PASSED" in run.stdout
