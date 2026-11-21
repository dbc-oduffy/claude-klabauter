"""Relay worker: agent output lines become mailbox lines, the worker exits with its child."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.agent_pipe import __main__ as relay
from coordinator_core.agent_pipe.contract import mailbox_path, run_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

FAKE = "coordinator_core.agent_pipe.tests.fixtures.fake_agent:FakeAdapter"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(relay.ADAPTER_ENV, FAKE)
    return tmp_path / "home"


def _request(run_id, **req):
    d = run_dir(run_id)
    d.mkdir(parents=True)
    (d / "request.json").write_text(json.dumps({"agent": "antigravity", "reply_to": "sess1", **req}), encoding="utf-8")
    return d


def _lines(session="sess1"):
    return [json.loads(x) for x in mailbox_path(session).read_text(encoding="utf-8").splitlines()]


LOADED_OWNER = "01a1a3e0-df64-7ce3-870d-b8c890ff9232"
LOADED_ERROR = "01a1a3da-39d3-7431-8d60-ef96caa764c9"


def test_worker_process_exits_with_its_child(home):
    d = _request("r9", prompt="hello")
    proc = subprocess.run(
        [sys.executable, "-m", "coordinator_core.agent_pipe", str(d)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[3]), os.environ.get("PYTHONPATH")]))},
        timeout=60,
        capture_output=True,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0
    assert [m["content"] for m in _lines()] == ["first", "second"]
    assert (d / "result.json").is_file()
