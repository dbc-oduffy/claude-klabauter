"""Relay worker: agent output lines become mailbox lines, the worker exits with its child."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.agent_pipe import __main__ as relay
from coordinator_core.agent_pipe import mailbox
from coordinator_core.agent_pipe.contract import mailbox_path, run_dir
from coordinator_core.workflow_watch.mailbox_line import wait_for_line

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


def test_two_unprompted_messages_land_and_result_is_written(home):
    d = _request("r1", prompt="hello")
    result = relay.run_worker(d)

    got = _lines()
    assert [m["content"] for m in got] == ["first", "second"]
    assert {m["sender_agent"] for m in got} == {"antigravity"}
    assert {m["sender_conversation"] for m in got} == {"fake-conv-1"}
    assert {m["to_session"] for m in got} == {"sess1"}
    assert result["status"] == "completed" and result["messages"] == 2 and result["conversation"] == "fake-conv-1"
    assert json.loads((d / "result.json").read_text(encoding="utf-8")) == result
    assert not list(d.glob("*.tmp"))


def test_wait_for_line_delivers_the_first_line(home):
    relay.run_worker(_request("r2", prompt="hello"))
    out = wait_for_line(str(mailbox_path("sess1")), from_start=True, poll_seconds=0.01)
    assert json.loads(out.splitlines()[0])["content"] == "first"


def test_resume_request_uses_resume_argv(home):
    d = _request("r3", conversation="conv-9", text="ping")
    relay.run_worker(d)
    argv = json.loads((d / "argv.json").read_text(encoding="utf-8"))
    assert argv[2:] == ["resume", "conv-9", "ping"]
    (msg,) = _lines()
    assert msg["content"] == "echo:ping" and msg["sender_conversation"] == "conv-9"


def test_append_after_drain_starts_a_fresh_mailbox_that_wakes_the_watcher(home):
    path = str(mailbox_path("sess1"))
    relay.run_worker(_request("r4", prompt="a"))
    woken = wait_for_line(path, from_start=True, poll_seconds=0.01)
    assert woken
    drained = mailbox.drain("sess1")
    assert len(drained) == 2

    relay.run_worker(_request("r5", prompt="b"))
    again = wait_for_line(path, from_start=True, poll_seconds=0.01)
    assert json.loads(again.splitlines()[0])["content"] == "first"


def test_argv_is_recorded_without_bypass_flags(home):
    d = _request("r6", prompt="hello")
    relay.run_worker(d)
    argv = json.loads((d / "argv.json").read_text(encoding="utf-8"))
    assert not any("dangerously" in a or "yolo" in a for a in argv)


def test_unknown_agent_leaves_a_failed_result(home, monkeypatch):
    monkeypatch.delenv(relay.ADAPTER_ENV)
    d = _request("r7", prompt="x")
    (d / "request.json").write_text(json.dumps({"agent": "nope", "reply_to": "sess1", "prompt": "x"}), encoding="utf-8")
    result = relay.run_worker(d)
    assert result["status"] == "failed" and "unknown agent" in result["error"]
    assert not mailbox_path("sess1").exists()


def test_missing_agent_binary_leaves_a_failed_result(home, monkeypatch):
    class Broken:
        def spawn_argv(self, prompt, conversation=None):
            return ["/nonexistent/agent-binary-xyz"]

        def parse_line(self, line):
            return None

    monkeypatch.setattr(relay, "load_adapter", lambda agent: Broken())
    result = relay.run_worker(_request("r8", prompt="x"))
    assert result["status"] == "failed" and result["error"]


def test_worker_process_exits_with_its_child(home):
    d = _request("r9", prompt="hello")
    proc = subprocess.run(
        [sys.executable, "-m", "coordinator_core.agent_pipe", str(d)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[3]), os.environ.get("PYTHONPATH")]))},
        timeout=60,
        capture_output=True,
    )
    assert proc.returncode == 0
    assert [m["content"] for m in _lines()] == ["first", "second"]
    assert (d / "result.json").is_file()
