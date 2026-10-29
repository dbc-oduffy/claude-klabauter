"""hash_worktree_blobs_via_spawn names the failure cause and first unhashed path."""
import pytest

from coordinator_core.git import commit as commit_mod
from coordinator_core.git.run import GitResult


def _patch(monkeypatch, result):
    monkeypatch.setattr("coordinator_core.git.run.run_git", lambda *a, **k: result)


def test_timeout_names_cause(monkeypatch, tmp_path):
    _patch(monkeypatch, GitResult(returncode=-1, stdout="", stderr="", timed_out=True))
    with pytest.raises(commit_mod.CommitRefused) as ei:
        commit_mod.hash_worktree_blobs_via_spawn(["a", "b"], cwd=tmp_path)
    assert ei.value.cause == "timed out"
    assert ei.value.first_unhashed == "a"
    assert ei.value.batch_count == 2


def test_partial_failure_names_first_unhashed(monkeypatch, tmp_path):
    _patch(monkeypatch, GitResult(returncode=128, stdout="sha1\n", stderr="fatal: boom\n", timed_out=False))
    with pytest.raises(commit_mod.CommitRefused) as ei:
        commit_mod.hash_worktree_blobs_via_spawn(["a", "b", "c"], cwd=tmp_path)
    assert ei.value.cause == "fatal: boom"
    assert ei.value.first_unhashed == "b"
    assert "b" in str(ei.value)


def test_empty_stderr_still_has_cause(monkeypatch, tmp_path):
    _patch(monkeypatch, GitResult(returncode=1, stdout="", stderr="", timed_out=False))
    with pytest.raises(commit_mod.CommitRefused) as ei:
        commit_mod.hash_worktree_blobs_via_spawn(["a"], cwd=tmp_path)
    assert "exit 1" in ei.value.cause


def test_success_unchanged(monkeypatch, tmp_path):
    _patch(monkeypatch, GitResult(returncode=0, stdout="s1\ns2\n", stderr="", timed_out=False))
    assert commit_mod.hash_worktree_blobs_via_spawn(["a", "b"], cwd=tmp_path) == {"a": "s1", "b": "s2"}
