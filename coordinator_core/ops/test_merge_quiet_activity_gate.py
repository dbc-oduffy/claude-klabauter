"""Tests for coordinator_core.ops.merge_quiet_activity_gate.

All git exercise runs against a throwaway repo created fresh under
`tmp_path` per test — NEVER against this working repo. See module
docstring for the op-key/contract this covers:
`merge.quiet_activity_gate`.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.merge_quiet_activity_gate import (
    _handler,
    _head_commit_epoch_seconds,
    _resolve_quiet_threshold_seconds,
    evaluate,
)


def _git(*args: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=env,
    )


@pytest.fixture(autouse=True)
def _isolate_global_git_config(tmp_path, monkeypatch):
    """Isolate from the ambient dev machine's global git config, mirroring
    the same fixture in test_detect_changed_dependency_manifests.py — this
    module's own subprocess calls inherit os.environ, so the override must
    be process-wide, not a one-off subprocess env=.
    """
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)


def _commit_file(root: Path, rel_path: str, content: str, *, when: str | None = None) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git("add", rel_path, cwd=root)
    env = None
    if when is not None:
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    _git("commit", "-q", "-m", f"add {rel_path}", cwd=root, env=env)


def test_not_a_git_repo_is_ok_with_message(tmp_path):
    root = tmp_path / "not-a-repo"
    root.mkdir()

    result = evaluate(root)

    assert result["ok"] is True
    assert result["seconds_since_last_commit"] == 0.0
    assert result["message"]


def test_no_commits_yet_is_ok_with_message(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)

    result = evaluate(root)

    assert result["ok"] is True
    assert result["seconds_since_last_commit"] == 0.0
    assert result["message"]


def test_recent_commit_blocks_gate_under_threshold(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    now = str(int(time.time()))
    _commit_file(root, "f.txt", "x", when=f"{now} +0000")

    result = evaluate(root, quiet_threshold_seconds=300)

    assert result["ok"] is False
    assert result["seconds_since_last_commit"] < 300
    assert "quiet threshold" in result["message"]


def test_old_commit_passes_gate_over_threshold(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    old_epoch = int(time.time()) - 3600
    _commit_file(root, "f.txt", "x", when=f"{old_epoch} +0000")

    result = evaluate(root, quiet_threshold_seconds=300)

    assert result["ok"] is True
    assert result["seconds_since_last_commit"] >= 300
    assert result["message"] is None


def test_future_commit_timestamp_clamps_to_zero_and_blocks(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    future_epoch = int(time.time()) + 3600
    _commit_file(root, "f.txt", "x", when=f"{future_epoch} +0000")

    result = evaluate(root, quiet_threshold_seconds=300)

    assert result["seconds_since_last_commit"] == 0.0
    assert result["ok"] is False


def test_double_invocation_is_idempotent(tmp_path):
    """AC7: a second invocation with identical inputs is a safe no-op —
    identical git state on rerun yields the identical `ok`/`message`
    verdict, no mutation performed. `seconds_since_last_commit` is a
    wall-clock-derived reading (not a stored value) so it legitimately
    ticks forward between the two calls; idempotency is about the VERDICT
    being stable given the same underlying git state, not the reading
    being frozen.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    old_epoch = int(time.time()) - 3600
    _commit_file(root, "f.txt", "x", when=f"{old_epoch} +0000")

    first = evaluate(root, quiet_threshold_seconds=300)
    second = evaluate(root, quiet_threshold_seconds=300)

    assert first["ok"] == second["ok"] is True
    assert first["message"] == second["message"] is None
    assert abs(first["seconds_since_last_commit"] - second["seconds_since_last_commit"]) < 5


def test_head_commit_epoch_seconds_helper_reads_committer_time(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    epoch = int(time.time()) - 120
    _commit_file(root, "f.txt", "x", when=f"{epoch} +0000")

    assert _head_commit_epoch_seconds(root) == float(epoch)


def test_head_commit_epoch_seconds_none_when_not_a_repo(tmp_path):
    root = tmp_path / "not-a-repo"
    root.mkdir()

    assert _head_commit_epoch_seconds(root) is None


@pytest.mark.parametrize(
    "raw, expected",
    [(None, 300), (300, 300), ("300", 300), ("bogus", 300), (60, 60)],
)
def test_resolve_quiet_threshold_seconds_coerces_and_falls_back(raw, expected):
    assert _resolve_quiet_threshold_seconds(raw) == expected


def test_handler_uses_dispatch_repo_root_and_default_threshold(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    old_epoch = int(time.time()) - 3600
    _commit_file(root, "f.txt", "x", when=f"{old_epoch} +0000")

    result = _handler({}, repo_root=root)

    assert result["ok"] is True
    assert result["message"] is None


def test_handler_honors_explicit_quiet_threshold_seconds_param(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    now = str(int(time.time()))
    _commit_file(root, "f.txt", "x", when=f"{now} +0000")

    result = _handler({"quiet_threshold_seconds": 300}, repo_root=root)

    assert result["ok"] is False
    assert result["seconds_since_last_commit"] < 300
