"""Tests for the PostToolUse(Bash) TaskStop launch recorder (tmp_path store only)."""

import os
import subprocess
import time

import pytest

from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.hooks import taskstop_launch_recorder as rec

BATCH = 100
BAR_MS = 500


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(rec.sys, "platform", "win32")
    return tmp_path


def _payload(**over):
    p = {
        "session_id": "sess1",
        "tool_use_id": "tu1",
        "tool_name": "Bash",
        "tool_input": {"command": "sleep 3001", "run_in_background": True},
        "tool_response": {"backgroundTaskId": "bg1abc"},
    }
    p.update(over)
    return p


def test_background_launch_writes_record():
    assert rec._handler({"payload": _payload()}) == {}
    r = store.read_record("bg1abc")
    assert (r.session_id, r.tool_use_id, r.command) == ("sess1", "tu1", "sleep 3001")
    assert abs((r.mark - rec._FILETIME_UNIX_EPOCH) / 1e7 - time.time()) < 60


def test_bare_params_without_payload_wrapper():
    rec._handler(_payload())
    assert store.read_record("bg1abc") is not None


@pytest.mark.parametrize(
    "payload",
    [
        _payload(tool_input={"command": "ls"}),
        _payload(tool_input={"command": "ls", "run_in_background": False}),
        _payload(tool_response={}),
        _payload(tool_response={"backgroundTaskId": 7}),
        _payload(tool_response={"backgroundTaskId": "../evil"}),
        _payload(tool_response="text"),
        _payload(tool_input=None),
        _payload(tool_name="Read"),
    ],
)
def test_non_launch_writes_nothing(payload):
    assert rec._handler({"payload": payload}) == {}
    assert store.all_records() == []


def test_non_windows_is_a_noop(monkeypatch):
    for plat in ("linux", "darwin"):
        monkeypatch.setattr(rec.sys, "platform", plat)
        assert rec._handler({"payload": _payload()}) == {}
        assert store.all_records() == []


def test_never_raises_on_garbage():
    for params in (None, 5, [], {"payload": 3}, {"payload": {"tool_name": "Bash", "tool_input": 1}}):
        assert rec._handler(params) == {}


def test_store_failure_is_swallowed(monkeypatch):
    def boom(_r):
        raise RuntimeError("disk")

    monkeypatch.setattr(store, "write_record", boom)
    assert rec._handler({"payload": _payload()}) == {}


def _spawn_trap(*_a, **_k):
    raise AssertionError("recorder spawned a process")


def test_cost_zero_spawns_and_under_bar(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "Popen", _spawn_trap)
    monkeypatch.setattr(os, "system", _spawn_trap)
    monkeypatch.setattr(os, "popen", _spawn_trap)
    payloads = [
        _payload(tool_response={"backgroundTaskId": f"bg{i}"}) for i in range(BATCH)
    ]
    t0 = time.process_time()
    for p in payloads:
        assert rec._handler({"payload": p}) == {}
    mean_ms = (time.process_time() - t0) * 1000 / BATCH
    assert len(store.all_records()) == BATCH
    with capsys.disabled():
        print(f"\nrecorder mean process time: {mean_ms:.2f} ms over {BATCH} writes")
    assert mean_ms < BAR_MS
