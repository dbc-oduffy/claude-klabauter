"""Characterization + parity tests for coordinator_core.ops.workday_start_step0_reconcile.

Port of: workday-start-step0-reconcile.sh (example-doctrine-repo b5a4192c, 2026-07-20), ~42 lines.
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.workday_start_step0_reconcile import main


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )


def _make_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Build a bare `origin` repo + a clone with `main` checked out and a
    commit, mirroring a real workday-start precondition."""
    # -b main pins the bare origin's default branch explicitly rather than
    # relying on the box's init.defaultBranch config (which may be "main"
    # or the legacy "master") -- without this, a clone of the (initially
    # empty) origin auto-checks-out whatever the box-default branch name
    # is, and a later `checkout -b main origin/main` either collides with
    # an already-checked-out local "main" or fails to find "origin/main"
    # at all, depending on the box.
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    # -B (not -b): the empty-origin clone may already have "main" checked
    # out (unborn) as its default branch; -B resets/creates it either way.
    _git(clone, "checkout", "-q", "-B", "main")
    (clone / "README.md").write_text("hello\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-q", "-m", "initial")
    _git(clone, "push", "-q", "-u", "origin", "main")
    return origin, clone


def test_not_a_git_repo_is_hard_error(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main([])
    err = capsys.readouterr().err
    assert rc != 0
    assert "not a git repository" in err.lower()


def test_already_current_when_origin_main_is_ancestor(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")

    monkeypatch.chdir(clone)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ALREADY-CURRENT branch=work/testmachine/2026-01-01" in out


def test_reconciled_ff_when_origin_advances_cleanly(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    (other / "upstream.txt").write_text("more\n")
    _git(other, "add", "upstream.txt")
    _git(other, "commit", "-q", "-m", "upstream advance")
    _git(other, "push", "-q", "origin", "main")

    monkeypatch.chdir(clone)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RECONCILED-FF branch=work/testmachine/2026-01-01" in out
    head = _git(clone, "rev-parse", "HEAD").stdout.strip()
    origin_head = _git(clone, "rev-parse", "origin/main").stdout.strip()
    assert head == origin_head


def test_reconciled_merge_when_both_sides_advance_without_conflict(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")
    (clone / "local-only.txt").write_text("local\n")
    _git(clone, "add", "local-only.txt")
    _git(clone, "commit", "-q", "-m", "local-only work")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    (other / "upstream.txt").write_text("more\n")
    _git(other, "add", "upstream.txt")
    _git(other, "commit", "-q", "-m", "upstream advance")
    _git(other, "push", "-q", "origin", "main")

    monkeypatch.chdir(clone)
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RECONCILED-MERGE branch=work/testmachine/2026-01-01" in out
    assert (clone / "upstream.txt").exists()
    assert (clone / "local-only.txt").exists()


def test_reconcile_conflict_aborts_and_exits_3(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")
    (clone / "README.md").write_text("local edit\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-q", "-m", "local conflicting edit")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    (other / "README.md").write_text("upstream edit\n")
    _git(other, "add", "README.md")
    _git(other, "commit", "-q", "-m", "upstream conflicting edit")
    _git(other, "push", "-q", "origin", "main")

    monkeypatch.chdir(clone)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-CONFLICT branch=work/testmachine/2026-01-01" in captured.out
    assert "Branch Reconciliation Decision" in captured.err

    status = _git(clone, "status", "--porcelain=v1")
    # merge --abort must have fully cleaned the working tree back up.
    assert status.stdout.strip() == ""
    merge_head = clone / ".git" / "MERGE_HEAD"
    assert not merge_head.exists()
