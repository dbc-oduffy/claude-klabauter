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


LOADED_OWNER = "01a1a3e0-df64-7ce3-870d-b8c890ff9232"
LOADED_ERROR = "01a1a3da-39d3-7431-8d60-ef96caa764c9"


def _app_server_adapter(monkeypatch, scenario):
    from coordinator_core.agent_pipe.tests.fixtures.fake_agent import FakeAdapter
    from coordinator_core.agent_pipe.tests.fixtures.fake_app_server import SCRIPT

    class WithProxy(FakeAdapter):
        def proxy_argv(self):
            return [sys.executable, SCRIPT, scenario]

    monkeypatch.setattr(relay, "load_adapter", lambda agent: WithProxy())


def test_loaded_thread_is_delivered_through_the_app_server(home, monkeypatch):
    _app_server_adapter(monkeypatch, "non_owner")
    d = _request("a1", agent="codex", conversation=LOADED_OWNER, text="ping")
    result = relay.run_worker(d)
    argv = json.loads((d / "argv.json").read_text(encoding="utf-8"))
    assert argv[-1] == "non_owner" and "resume" not in argv
    (msg,) = _lines()
    assert msg["content"] == "CROSS" and msg["sender_conversation"] == LOADED_OWNER
    assert result["status"] == "completed" and result["messages"] == 1 and result["error"] is None


def test_thread_not_loaded_falls_back_to_exec_resume(home, monkeypatch):
    _app_server_adapter(monkeypatch, "owner")
    d = _request("a2", conversation="headless-conv", text="ping")
    result = relay.run_worker(d)
    argv = json.loads((d / "argv.json").read_text(encoding="utf-8"))
    assert argv[2:] == ["resume", "headless-conv", "ping"]
    assert result["status"] == "completed"
    assert [m["content"] for m in _lines()] == ["echo:ping"]


def test_unreachable_app_server_falls_back_to_exec_resume(home, monkeypatch):
    _app_server_adapter(monkeypatch, "dead")
    d = _request("a3", conversation=LOADED_OWNER, text="ping")
    result = relay.run_worker(d)
    argv = json.loads((d / "argv.json").read_text(encoding="utf-8"))
    assert argv[2:] == ["resume", LOADED_OWNER, "ping"]
    assert result["status"] == "completed"


def test_loaded_thread_turn_start_error_fails_without_exec_resume(home, monkeypatch):
    _app_server_adapter(monkeypatch, "error")
    d = _request("a4", conversation=LOADED_ERROR, text="ping")
    result = relay.run_worker(d)
    argv = json.loads((d / "argv.json").read_text(encoding="utf-8"))
    assert "resume" not in argv and argv[-1] == "error"
    assert result["status"] == "failed" and "thread not found" in result["error"]
    assert result["messages"] == 0
    assert not mailbox_path("sess1").exists()


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


def test_help_flag_prints_usage_and_never_runs_a_worker(capsys, tmp_path, monkeypatch):
    # `--help` was once taken as a run_dir and crashed writing '--help/result.json'.
    from coordinator_core.agent_pipe import __main__ as cli

    monkeypatch.chdir(tmp_path)
    assert cli.main(["--help"]) == 0
    assert "usage:" in capsys.readouterr().out
    assert cli.main(["--bogus"]) == 2
    assert list(tmp_path.iterdir()) == []
