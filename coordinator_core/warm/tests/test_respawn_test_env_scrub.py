"""A warm-server spawn from a test-marked process never becomes the box's warm server.

Purpose: pins the three closures of the 2026-10-29 poisoning -- the client declines, the single
`detached_spawn` choke point refuses warm-script spawns from a test-marked process, and the
server and supervisor scrub test-only variables at boot so `settings_home()` cannot raise.
Every path resolves under `tmp_path`; no live server is touched.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core import _settings_home
from coordinator_core.ops.ceremony import detached_spawn
from coordinator_core.warm import client, supervisor, server

FORBID = _settings_home.FORBID_REAL_SETTINGS_HOME_ENV


@pytest.fixture
def popen_calls(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    return calls


def _warm_repo(tmp_path):
    script = tmp_path / "coordinator_core" / "warm" / "server.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    return str(tmp_path), "coordinator_core/warm/server.py"


def test_try_warm_dispatch_declines_on_the_forbid_var_alone(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("COORDINATOR_WARM_RUNTIME_BASE", raising=False)
    monkeypatch.setenv(FORBID, "1")
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    spawned: list = []
    monkeypatch.setattr(client, "spawn_detached", lambda *a, **k: spawned.append(a) or True)

    def _unreachable():
        raise AssertionError("decline must precede any engine identity or pipe work")

    monkeypatch.setattr(client, "engine_token", _unreachable)
    assert client.try_warm_dispatch({"op": "ping"}) is None
    assert spawned == []


def test_a_test_isolated_runtime_base_is_still_served(tmp_path, monkeypatch):
    monkeypatch.setenv(FORBID, "1")
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "rt"))
    assert client.process_is_test_marked()
    reached = []
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: reached.append(1) or "unversioned")
    client.try_warm_dispatch({"op": "ping"})
    assert reached == [1]


def test_warm_script_spawn_from_a_test_is_refused(tmp_path, monkeypatch, popen_calls):
    repo, script = _warm_repo(tmp_path)
    monkeypatch.setenv(FORBID, "1")
    monkeypatch.delenv("COORDINATOR_WARM_RUNTIME_BASE", raising=False)
    assert detached_spawn.spawn_detached(repo, script) is False
    assert popen_calls == []


def test_warm_script_spawn_with_an_isolated_runtime_base_proceeds(tmp_path, monkeypatch, popen_calls):
    repo, script = _warm_repo(tmp_path)
    monkeypatch.setenv(FORBID, "1")
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "rt"))
    assert detached_spawn.spawn_detached(repo, script) is True
    assert len(popen_calls) == 1
    assert FORBID not in popen_calls[0][1]["env"]


def test_a_non_warm_script_spawn_from_a_test_is_not_refused(tmp_path, monkeypatch, popen_calls):
    other = tmp_path / "tool.py"
    other.write_text("", encoding="utf-8")
    monkeypatch.setenv(FORBID, "1")
    assert detached_spawn.spawn_detached(str(tmp_path), "tool.py") is True
    assert len(popen_calls) == 1


def test_every_delegate_reaches_the_one_choke_point(tmp_path, monkeypatch, popen_calls):
    from coordinator_core.warm import front_door

    repo, script = _warm_repo(tmp_path)
    monkeypatch.setenv(FORBID, "1")
    monkeypatch.delenv("COORDINATOR_WARM_RUNTIME_BASE", raising=False)
    for delegate in (client.spawn_detached, front_door.spawn_detached, supervisor.spawn_detached):
        assert delegate(repo, script) is False
    assert popen_calls == []


@pytest.mark.parametrize("module", [server, supervisor], ids=["server", "supervisor"])
def test_process_entry_scrubs_test_vars_and_settings_home_resolves(module, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", os.environ.get("PYTEST_CURRENT_TEST", "t"))
    for key in [k for k in os.environ if client.is_test_env_key(k)]:
        monkeypatch.setenv(key, os.environ[key])  # recorded, so teardown restores it
    monkeypatch.setenv(FORBID, "1")
    monkeypatch.setenv("COORDINATOR_PYTEST_X", "1")
    monkeypatch.setattr(module, "main", lambda: 0)
    assert module._process_entry() == 0
    assert not [k for k in os.environ if client.is_test_env_key(k)]
    _settings_home.settings_home()


class _FakeFh:
    """A client-end stand-in: records the frame written, answers one scripted line."""

    def __init__(self, reply: bytes, on_read=None):
        self.reply = reply
        self.on_read = on_read
        self.written = b""

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def readline(self):
        if self.on_read:
            self.on_read()
        return self.reply

    def close(self):
        pass


class _Retoken:
    """Scripts a publish handover: the engine token, the per-pipe open outcomes."""

    def __init__(self, monkeypatch, mutates: bool):
        self.token = "t1"
        self.opens: list = []
        self.script: dict = {}
        self.drained = 0
        monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
        monkeypatch.setattr(client, "process_is_test_marked", lambda: False)
        monkeypatch.setattr(client, "_await_stage_admission", lambda *a, **k: None)
        monkeypatch.setattr(client, "_configured_root_is_foreign", lambda: False)
        monkeypatch.setattr(client, "_clear_unanswered", lambda: None)
        monkeypatch.setattr(client, "_live_tree_cold", False)
        monkeypatch.setattr(client, "_op_may_mutate", lambda _m: mutates)
        monkeypatch.setattr(client, "engine_token", lambda: self.token)
        monkeypatch.setattr(client, "_endpoint_name", lambda t: f"pipe-{t}")
        monkeypatch.setattr(client, "_open_pipe", self._open)
        monkeypatch.setattr(client, "_drain_publish_hold", self._drain)

    def _open(self, pipe):
        self.opens.append(pipe)
        outcome = self.script[pipe].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def _drain(self, hook_path):
        self.drained += 1
        return None

    def flip(self):
        self.token = "t2"


MSG = {"jsonrpc": "2.0", "id": 1, "method": "demo.op", "params": {}}
OK = b'{"jsonrpc": "2.0", "id": 1, "result": 7}\n'


def test_enoent_after_a_token_flip_reopens_under_the_new_token(monkeypatch):
    r = _Retoken(monkeypatch, mutates=True)
    fh = _FakeFh(OK)
    r.script = {"pipe-t1": [FileNotFoundError()], "pipe-t2": [fh]}

    def _open(pipe):
        if pipe == "pipe-t1":
            r.flip()
        return r._open(pipe)

    monkeypatch.setattr(client, "_open_pipe", _open)
    assert client._try_warm_dispatch_inner(dict(MSG))["result"] == 7
    assert r.opens == ["pipe-t1", "pipe-t2"]
    assert b'"_engine_token": "t2"' in fh.written
    assert r.drained == 0


def test_enoent_with_an_unmoved_token_goes_cold_without_a_second_open(monkeypatch):
    r = _Retoken(monkeypatch, mutates=True)
    r.script = {"pipe-t1": [FileNotFoundError()]}
    assert client._try_warm_dispatch_inner(dict(MSG)) is None
    assert r.opens == ["pipe-t1"]
    assert r.drained == 1


def test_zero_byte_close_after_a_flip_resends_a_compute_only_frame(monkeypatch):
    r = _Retoken(monkeypatch, mutates=False)
    second = _FakeFh(OK)
    r.script = {"pipe-t1": [_FakeFh(b"", on_read=r.flip)], "pipe-t2": [second]}
    assert client._try_warm_dispatch_inner(dict(MSG))["result"] == 7
    assert r.opens == ["pipe-t1", "pipe-t2"]
    assert b'"_engine_token": "t2"' in second.written


def test_zero_byte_close_after_a_flip_resends_a_mutation_to_the_successor(monkeypatch):
    r = _Retoken(monkeypatch, mutates=True)
    r.script = {"pipe-t1": [_FakeFh(b"", on_read=r.flip)], "pipe-t2": [_FakeFh(OK)]}
    assert client._try_warm_dispatch_inner(dict(MSG))["result"] == 7
    assert r.opens == ["pipe-t1", "pipe-t2"]


def test_zero_byte_close_with_an_unmoved_token_never_resends_a_mutation(monkeypatch):
    r = _Retoken(monkeypatch, mutates=True)
    r.script = {"pipe-t1": [_FakeFh(b"")], "pipe-t2": [_FakeFh(OK)]}
    assert client._try_warm_dispatch_inner(dict(MSG)) is None
    assert r.opens == ["pipe-t1"]


def test_skew_refusal_after_a_flip_resends_once_even_for_a_mutation(monkeypatch):
    skew = (
        b'{"jsonrpc": "2.0", "id": 1, "error": {"code": %d, "message": "skew"}}\n' % client.ENGINE_SKEW
    )
    r = _Retoken(monkeypatch, mutates=True)
    r.script = {"pipe-t1": [_FakeFh(skew, on_read=r.flip)], "pipe-t2": [_FakeFh(OK)]}
    assert client._try_warm_dispatch_inner(dict(MSG))["result"] == 7
    assert r.opens == ["pipe-t1", "pipe-t2"]


def test_skew_refusal_with_an_unmoved_token_goes_cold(monkeypatch):
    skew = (
        b'{"jsonrpc": "2.0", "id": 1, "error": {"code": %d, "message": "skew"}}\n' % client.ENGINE_SKEW
    )
    r = _Retoken(monkeypatch, mutates=True)
    r.script = {"pipe-t1": [_FakeFh(skew)], "pipe-t2": [_FakeFh(OK)]}
    assert client._try_warm_dispatch_inner(dict(MSG)) is None
    assert r.opens == ["pipe-t1"]
