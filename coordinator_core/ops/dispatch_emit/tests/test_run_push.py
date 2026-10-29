"""Tests for run_push.nudge_push: queues owned work/* branches, refuses the rest."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import run_push


@pytest.fixture
def calls(monkeypatch):
    seen = []
    from coordinator_core.warm import push_cadence

    monkeypatch.setattr(push_cadence, "note_commit", lambda r: seen.append(r))

    def no_spawn(*a, **k):
        raise AssertionError("no process may be spawned")

    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    return seen


def _setup(monkeypatch, branch, remote="origin", skipped=("push:decide-only",), owned=True):
    monkeypatch.setattr(run_push, "head_branch", lambda r: branch)
    monkeypatch.setattr(
        run_push, "_owned_remote", lambda r, b: (remote, None) if owned else (None, "remote origin is non-owned")
    )
    from coordinator_core.ops import push_outstanding as po

    monkeypatch.setattr(po, "push_outstanding", lambda w, decide_only=False: SimpleNamespace(skipped=list(skipped)))


def test_work_branch_owned_remote_nudges(monkeypatch, tmp_path, calls):
    _setup(monkeypatch, "work/machine-a/2026-10-28")
    r = run_push.nudge_push(tmp_path, tmp_path)
    assert r["status"] == "nudged"
    assert "queued" in r["reason"] and "not yet pushed" in r["reason"]
    assert calls == [tmp_path]


def test_main_not_pushed(monkeypatch, tmp_path, calls):
    _setup(monkeypatch, "main")
    r = run_push.nudge_push(tmp_path, tmp_path)
    assert r["status"] == "not_pushed" and "work/*" in r["reason"]
    assert calls == []


def test_non_owned_remote_not_pushed(monkeypatch, tmp_path, calls):
    _setup(monkeypatch, "work/a/b", owned=False)
    r = run_push.nudge_push(tmp_path, tmp_path)
    assert r["status"] == "not_pushed" and "non-owned" in r["reason"]
    assert calls == []


def test_nothing_ahead_not_pushed(monkeypatch, tmp_path, calls):
    _setup(monkeypatch, "work/a/b", skipped=("push:nothing-outstanding",))
    r = run_push.nudge_push(tmp_path, tmp_path)
    assert r["status"] == "not_pushed" and "nothing ahead" in r["reason"]
    assert calls == []


def test_detached_head_not_pushed(monkeypatch, tmp_path, calls):
    _setup(monkeypatch, None)
    assert run_push.nudge_push(tmp_path, tmp_path)["status"] == "not_pushed"
    assert calls == []
