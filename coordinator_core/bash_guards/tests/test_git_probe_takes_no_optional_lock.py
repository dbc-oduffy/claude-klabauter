"""A guard's git probe must never take an optional lock: a probe killed at its timeout while
`git diff` refreshed the index left a 0-byte index.lock that refused the user's next `git add`
(example-stats-repo, 2026-11-02)."""

from __future__ import annotations

import subprocess

from coordinator_core.bash_guards import _dc_git_probe


def _capture_env(monkeypatch):
    seen: dict = {}

    def _fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(_dc_git_probe.subprocess, "run", _fake_run)
    return seen


def test_probe_child_runs_with_optional_locks_off(monkeypatch):
    seen = _capture_env(monkeypatch)
    _dc_git_probe._run_git(["diff", "--name-only"])
    assert seen["env"]["GIT_OPTIONAL_LOCKS"] == "0"


def test_extra_env_still_applies_alongside(monkeypatch):
    seen = _capture_env(monkeypatch)
    monkeypatch.setenv("COORD_PROBE_DROP_ME", "x")
    _dc_git_probe._run_git(
        ["diff", "--cached"], extra_env={"GIT_INDEX_FILE": "alt", "COORD_PROBE_DROP_ME": None}
    )
    assert seen["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert seen["env"]["GIT_INDEX_FILE"] == "alt"
    assert "COORD_PROBE_DROP_ME" not in seen["env"]

