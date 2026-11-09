"""`apply._delete_branch` gate: tip ancestor of HEAD, no live holder, never refs/preserve/*."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.consolidate_assemble import apply as apply_mod
from coordinator_core.consolidate_assemble import live_holders as lh
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    )


def _branches(root: Path) -> set[str]:
    return set(_git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split())


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "work/me")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "branch", "merged")
    _git(root, "checkout", "-q", "-b", "unmerged")
    (root / "b.txt").write_text("x\n", encoding="utf-8", newline="\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-q", "-m", "unmerged work")
    _git(root, "checkout", "-q", "work/me")
    return root


def _free(monkeypatch, answer):
    monkeypatch.setattr(lh, "_cross_box_holders", lambda run_git, root, branches: {b: answer for b in branches})


def test_reachable_unheld_branch_is_deleted(repo, monkeypatch):
    _free(monkeypatch, [])
    apply_mod._delete_branch("merged", False, repo)
    assert "merged" not in _branches(repo)


def test_unreachable_tip_is_refused(repo, monkeypatch):
    _free(monkeypatch, [])
    with pytest.raises(RuntimeError, match="not an ancestor of HEAD"):
        apply_mod._delete_branch("unmerged", False, repo)
    assert "unmerged" in _branches(repo)


def test_no_holder_reachable_branch_is_deleted_without_override(repo):
    apply_mod._delete_branch("merged", False, repo)
    assert "merged" not in _branches(repo)


def test_unreachable_tip_is_refused_without_override(repo):
    with pytest.raises(RuntimeError, match="not an ancestor of HEAD"):
        apply_mod._delete_branch("unmerged", False, repo)
    assert "unmerged" in _branches(repo)


def test_recently_pushed_remote_ref_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OVERRIDE_BRANCH", raising=False)
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    root = tmp_path / "repo2"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "branch", "feat")
    _git(root, "push", "-q", "origin", "feat")
    with pytest.raises(RuntimeError, match="held by recent push"):
        apply_mod._delete_branch("feat", True, root)
    assert "feat" in _branches(root)


def test_failed_local_holder_probe_is_refused_unknown(repo, monkeypatch):
    monkeypatch.setattr(lh, "_local_holders", lambda run_git, root: None)
    with pytest.raises(RuntimeError, match="refused-unknown"):
        apply_mod._delete_branch("merged", False, repo)
    assert "merged" in _branches(repo)


def test_worktree_held_branch_is_refused(repo, monkeypatch, tmp_path):
    _free(monkeypatch, [])
    _git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "merged")
    with pytest.raises(RuntimeError, match="held by"):
        apply_mod._delete_branch("merged", False, repo)
    assert "merged" in _branches(repo)


def test_override_waives_unknown_holder_but_not_a_definite_one(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BRANCH", "merged")
    _git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "merged")
    _free(monkeypatch, [])
    with pytest.raises(RuntimeError, match="held by"):
        apply_mod._delete_branch("merged", False, repo)
    _git(repo, "worktree", "remove", "--force", str(tmp_path / "wt"))
    monkeypatch.undo()
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BRANCH", "merged")
    apply_mod._delete_branch("merged", False, repo)
    assert "merged" not in _branches(repo)


def test_preserve_ref_is_refused_even_with_override(repo, monkeypatch):
    _free(monkeypatch, [])
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BRANCH", "preserve/x")
    with pytest.raises(RuntimeError, match="refs/preserve"):
        apply_mod._delete_branch("preserve/x", False, repo)
