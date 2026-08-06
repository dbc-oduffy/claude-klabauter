"""Characterization + parity tests for coordinator_core.ops.sync_main.

Port of: sync-main.sh (example-doctrine-repo b5a4192c, 2026-07-20), ~124 lines.
Spec backlink: archive/specs/2026-05-01-orphan-branch-prevention.md § 1.1.5
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.sync_main import main


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
    commit, mirroring a real work-branch-creation-site precondition."""
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


def test_not_a_git_repo_exits_0_silently(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


def test_help_exits_0_and_prints_usage(capsys):
    rc = main(["--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Usage: sync-main.sh [OPTIONS]" in out
    assert "--strict" in out


def test_unknown_argument_exits_1(capsys):
    rc = main(["--bogus"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Unknown argument: --bogus" in err


def test_origin_unreachable_skips_silently(tmp_path, capsys, monkeypatch):
    _git_init = subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 0
    assert "origin/main not reachable" in err


def test_on_main_ff_pull_succeeds(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    (other / "file2.txt").write_text("more\n")
    _git(other, "add", "file2.txt")
    _git(other, "commit", "-q", "-m", "second")
    _git(other, "push", "-q", "origin", "main")

    monkeypatch.chdir(clone)
    rc = main(["--quiet"])
    assert rc == 0
    head = _git(clone, "rev-parse", "main").stdout.strip()
    origin_head = _git(clone, "rev-parse", "origin/main").stdout.strip()
    assert head == origin_head


def test_on_main_ahead_of_origin_is_hard_error(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    (clone / "local.txt").write_text("local only\n")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "-q", "-m", "local ahead commit")

    monkeypatch.chdir(clone)
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 1
    assert "ahead of origin/main" in err


def test_on_other_branch_updates_local_main_ref(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    _git(other, "checkout", "-q", "-B", "main", "origin/main")
    (other / "file2.txt").write_text("more\n")
    _git(other, "add", "file2.txt")
    _git(other, "commit", "-q", "-m", "second")
    _git(other, "push", "-q", "origin", "main")

    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")

    monkeypatch.chdir(clone)
    rc = main(["--quiet"])
    assert rc == 0
    local_main = _git(clone, "rev-parse", "main").stdout.strip()
    origin_main = _git(clone, "rev-parse", "origin/main").stdout.strip()
    assert local_main == origin_main


def test_on_other_branch_local_main_ahead_is_hard_error(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)
    (clone / "local.txt").write_text("local only\n")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "-q", "-m", "local ahead commit")

    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")

    monkeypatch.chdir(clone)
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 1
    assert "ahead of origin/main" in err


def test_strict_hard_errors_when_far_behind_main(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)

    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")

    for i in range(51):
        (clone / f"f{i}.txt").write_text(f"{i}\n")
        _git(clone, "checkout", "-q", "main")
        _git(clone, "add", f"f{i}.txt")
        _git(clone, "commit", "-q", "-m", f"commit {i}")
        _git(clone, "checkout", "-q", "work/testmachine/2026-01-01")
    _git(clone, "push", "-q", "origin", "main")

    monkeypatch.chdir(clone)
    rc = main(["--strict"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "commits behind main" in err


def test_non_strict_warns_when_far_behind_main(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)

    _git(clone, "checkout", "-q", "-b", "work/testmachine/2026-01-01")

    for i in range(51):
        (clone / f"f{i}.txt").write_text(f"{i}\n")
        _git(clone, "checkout", "-q", "main")
        _git(clone, "add", f"f{i}.txt")
        _git(clone, "commit", "-q", "-m", f"commit {i}")
        _git(clone, "checkout", "-q", "work/testmachine/2026-01-01")
    _git(clone, "push", "-q", "origin", "main")

    monkeypatch.chdir(clone)
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 0
    assert "WARNING" in err
    assert "commits behind main" in err
