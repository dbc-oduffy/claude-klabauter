"""agent_pipe.start / agent_pipe.send op tests: validation, spawn shape, and one real detached round trip."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from coordinator_core.agent_pipe.contract import mailbox_path
from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import agent_pipe as op
from coordinator_core.workflow_watch.mailbox_line import wait_for_line

FAKE = "coordinator_core.agent_pipe.tests.fixtures.fake_agent:FakeAdapter"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class _FakeProc:
    pid = 4242


@pytest.fixture
def popen(monkeypatch):
    calls = []

    def fake(argv, **kw):
        calls.append((argv, kw))
        return _FakeProc()

    monkeypatch.setattr(op.subprocess, "Popen", fake)
    return calls


def test_start_writes_request_and_spawns_detached(home, popen):
    out = op._agent_pipe_start({"agent": "antigravity", "prompt": "hi", "reply_to": "s1"})

    run_dir = home / "agent-pipe" / "runs" / out["run_id"]
    assert out["run_id"].startswith("antigravity-") and out["pid"] == 4242
    assert out["mailbox"] == str(home / "agent-pipe" / "mailboxes" / "s1.jsonl")
    assert json.loads((run_dir / "request.json").read_text(encoding="utf-8")) == {
        "agent": "antigravity",
        "reply_to": "s1",
        "prompt": "hi",
    }
    argv, kw = popen[0]
    assert argv[1:] == ["-m", "coordinator_core.agent_pipe", str(run_dir)]
    assert kw.get("start_new_session") is True or "creationflags" in kw
    assert kw["stdin"] is subprocess.DEVNULL


def test_send_records_conversation_and_text(home, popen):
    out = op._agent_pipe_send({"agent": "codex", "conversation": "c1", "text": "yo", "reply_to": "s1"})
    req = json.loads((home / "agent-pipe" / "runs" / out["run_id"] / "request.json").read_text(encoding="utf-8"))
    assert req == {"agent": "codex", "reply_to": "s1", "conversation": "c1", "text": "yo"}


@pytest.mark.parametrize(
    "bad",
    [
        {"agent": "bogus", "prompt": "x", "reply_to": "s"},
        {"agent": "codex", "reply_to": "s"},
        {"agent": "codex", "prompt": "  ", "reply_to": "s"},
        {"agent": "codex", "prompt": "x"},
        {"agent": "codex", "prompt": "x", "reply_to": "../escape"},
        {"agent": "codex", "prompt": "x", "reply_to": "s", "cwd": "/nonexistent/dir/xyz"},
        {"agent": "codex", "prompt": 5, "reply_to": "s"},
    ],
)
def test_start_rejects_bad_params_without_spawning(home, popen, bad):
    with pytest.raises(CallerFacingValidationError):
        op._agent_pipe_start(bad)
    assert popen == []


def test_send_requires_conversation_and_text(home, popen):
    with pytest.raises(CallerFacingValidationError):
        op._agent_pipe_send({"agent": "codex", "text": "x", "reply_to": "s"})
    with pytest.raises(CallerFacingValidationError):
        op._agent_pipe_send({"agent": "codex", "conversation": "c", "reply_to": "s"})


def test_wire_cannot_inject_argv(home, popen):
    out = op._agent_pipe_start({"agent": "codex", "prompt": "x", "reply_to": "s", "argv": ["rm"]})
    req = json.loads((home / "agent-pipe" / "runs" / out["run_id"] / "request.json").read_text(encoding="utf-8"))
    assert "argv" not in req


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_default_start_round_trip_wakes_and_leaves_no_worker(home, monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_PIPE_ADAPTER", FAKE)
    out = op._agent_pipe_start({"agent": "antigravity", "prompt": "hello", "reply_to": "sess"})

    woken = wait_for_line(out["mailbox"], from_start=True, poll_seconds=0.05)
    assert json.loads(woken.splitlines()[0])["content"] == "first"

    result = home / "agent-pipe" / "runs" / out["run_id"] / "result.json"
    deadline = time.time() + 30
    while not result.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert json.loads(result.read_text(encoding="utf-8"))["messages"] == 2
    assert len(mailbox_path("sess").read_text(encoding="utf-8").splitlines()) == 2

    if os.name == "nt":
        return  # os.kill(pid, 0) terminates on Windows; liveness cannot be probed this way
    deadline = time.time() + 10
    while _alive(out["pid"]) and time.time() < deadline:
        try:
            os.waitpid(out["pid"], os.WNOHANG)  # reap the zombie our own Popen left
        except ChildProcessError:
            pass
        time.sleep(0.05)
    assert not _alive(out["pid"])
