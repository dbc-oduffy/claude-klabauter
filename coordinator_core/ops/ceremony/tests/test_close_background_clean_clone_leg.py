"""The clean-clone-proof close-background leg spawns the runner detached and never waits."""

from __future__ import annotations

import subprocess
import sys

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.clean_clone_proof_contract import RUNNER_MODULE
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit import terminal_commit_closeout as tcc

SHA = "a" * 40


def _ctx(root, sha=SHA):
    return cb.JobContext(root, root / ".git", "run-x", (), None, terminal_sha=sha)


def test_leg_is_ordered_after_close_commit_and_registered_in_both_ledgers():
    legs = cb.BACKGROUND_LEGS
    assert legs.index("clean-clone-proof") == legs.index("close-commit") + 1
    assert "clean-clone-proof" in cb.DEFAULT_RUNNERS
    assert "clean-clone-proof" in tcc._BACKGROUND_LEGS
    assert "clean-clone-proof" in run_close.RUN_BOUND_LEGS


def test_no_terminal_sha_is_not_applicable_and_spawns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    result = cb.DEFAULT_RUNNERS["clean-clone-proof"](_ctx(tmp_path, sha=None))
    assert result.status == "not-applicable"


def test_terminal_sha_spawns_the_runner_module_detached(tmp_path, monkeypatch):
    calls = []

    class _Proc:
        def wait(self, *a, **k):
            raise AssertionError("the leg waited on the runner")

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)) or _Proc())
    result = cb.DEFAULT_RUNNERS["clean-clone-proof"](_ctx(tmp_path))
    assert result.status == "ran"
    (argv, kw), = calls
    assert argv == [sys.executable, "-m", RUNNER_MODULE, "--repo", str(tmp_path), "--run-id", "run-x"]
    assert kw["stdin"] is subprocess.DEVNULL and kw["stdout"] is subprocess.DEVNULL
    assert kw["start_new_session"] is True or "creationflags" in kw


def test_a_spawn_failure_is_this_legs_failure(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr(subprocess, "Popen", boom)
    result = cb.DEFAULT_RUNNERS["clean-clone-proof"](_ctx(tmp_path))
    assert result.status == "failed" and "OSError" in result.detail
