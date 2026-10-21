"""`apply._dispatch_merge_and_delete` never leaves a merge in progress.

A merge that stops after staging its result -- the message editor failing
with no terminal, a rejecting hook, a conflict -- left `MERGE_HEAD` in a
shared tree, and the next committer's commit consumed it as a merge commit.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.consolidate_assemble import apply as apply_mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
        check=check, **no_console_creationflags(),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "work/me")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "checkout", "-q", "-b", "work/sibling")
    (root / "b.txt").write_text("sibling\n", encoding="utf-8", newline="\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-q", "-m", "sibling")
    _git(root, "checkout", "-q", "work/me")
    (root / "c.txt").write_text("mine\n", encoding="utf-8", newline="\n")
    _git(root, "add", "c.txt")
    _git(root, "commit", "-q", "-m", "mine")
    return root


def test_merges_without_an_editor(repo, monkeypatch):
    monkeypatch.setenv("GIT_EDITOR", "false")
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BRANCH", "work/sibling")
    apply_mod._dispatch_merge_and_delete(["work/sibling", "work/sibling"], repo)
    assert (repo / "b.txt").exists()
    assert not (repo / ".git" / "MERGE_HEAD").exists()


def test_a_merge_stopped_after_staging_is_aborted(repo):
    hooks = repo.parent / "hooks"
    hooks.mkdir()
    (hooks / "commit-msg").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    (hooks / "commit-msg").chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(Exception):
        apply_mod._dispatch_merge_and_delete(["work/sibling", "work/sibling"], repo)

    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


@pytest.fixture()
def share_repo(repo: Path) -> Path:
    _git(repo, "checkout", "-q", "work/sibling")
    share = repo / ".coordinator-local" / "subagent-share" / "s1"
    share.mkdir(parents=True)
    (share / "r.md").write_text("x\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-f", ".coordinator-local/subagent-share/s1/r.md")
    _git(repo, "commit", "-q", "-m", "adds share")
    _git(repo, "checkout", "-q", "work/me")
    return repo


def test_merge_only_refuses_a_ref_adding_machine_local_paths(share_repo):
    head = _git(share_repo, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(RuntimeError, match="machine-local path"):
        apply_mod._dispatch_merge_only(["work/sibling", "work/sibling"], share_repo)
    assert _git(share_repo, "rev-parse", "HEAD").stdout.strip() == head
    assert not (share_repo / ".git" / "MERGE_HEAD").exists()


def test_merge_and_delete_refusal_keeps_the_branch(share_repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BRANCH", "work/sibling")
    with pytest.raises(RuntimeError, match="machine-local path"):
        apply_mod._dispatch_merge_and_delete(["work/sibling", "work/sibling"], share_repo)
    assert _git(share_repo, "rev-parse", "--verify", "work/sibling").returncode == 0
