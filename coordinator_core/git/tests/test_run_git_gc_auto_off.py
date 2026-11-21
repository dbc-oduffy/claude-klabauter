"""run_git / run_git_stream carry `-c gc.auto=0` on every spawned argv.

Pins `coordinator_core/git/run.py :: _GC_AUTO_OFF`: the guarantee is per-invocation argv, so
auto-gc cannot start inside an engine landing even where the repo config sets `gc.auto` high or
was never written. The behavioural test builds a repo with `gc.auto=1` and loose objects past the
threshold; its positive control (a bare git commit packs) proves the fixture can fail.
"""

from __future__ import annotations

import subprocess

import pytest


from coordinator_core.git import run as run_mod
from coordinator_core.git.run import run_git, run_git_stream

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _spy_popen(monkeypatch) -> list:
    seen: list = []
    real = subprocess.Popen

    def spy(argv, **kw):
        seen.append(list(argv))
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return seen


def test_run_git_argv_carries_the_override_right_after_the_binary(monkeypatch):
    seen = _spy_popen(monkeypatch)
    assert run_git(["--version"]).ok
    assert seen[0][0] == run_mod._git_exe()
    assert seen[0][1:4] == ["-c", "gc.auto=0", "--version"]


def test_run_git_stream_argv_carries_the_override(monkeypatch):
    seen = _spy_popen(monkeypatch)
    with run_git_stream(["--version"]) as proc:
        proc.stdout.read()
    assert seen[0][1:4] == ["-c", "gc.auto=0", "--version"]


def test_override_does_not_change_leaf_classification():
    assert run_mod._is_leaf_argv(["rev-parse", "HEAD"])
    assert not run_mod._is_leaf_argv(["commit", "-m", "x"])
