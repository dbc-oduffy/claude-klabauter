"""Tests for envelope.main — the check-shipped-on-main.sh CLI port.

Port of: check-shipped-on-main.sh (example-doctrine-repo b5a4192c, 2026-07-20). Exercises the
exit-code/stdout contract (0/1/2, --verbose ON_MAIN/NOT_ON_MAIN lines) against
a real tmp git repo + bare "origin" remote, mirroring the bash script's own
semantics (git merge-base --is-ancestor against origin/main).

Spec backlink: archive/specs/2026-05-01-orphan-branch-prevention.md § 1.2
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.emit.envelope import main


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def repo_with_origin(tmp_path, monkeypatch):
    """A working repo with a real "origin" remote and an on-main + off-main commit.

    Layout:
      - bare_origin/         — bare repo acting as "origin"
      - work/                — clone; origin/main tracks bare_origin's main
        - first commit  -> pushed to origin/main (ON_MAIN)
        - second commit -> local-only, NOT pushed (NOT_ON_MAIN)
    """
    bare = tmp_path / "bare_origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "remote", "add", "origin", str(bare))

    (work / "a.txt").write_text("one\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-m", "first")
    on_main_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "push", "origin", "main")

    (work / "b.txt").write_text("two\n")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-m", "second (unpushed)")
    off_main_sha = _git(work, "rev-parse", "HEAD").stdout.strip()

    _git(work, "fetch", "origin")

    monkeypatch.chdir(work)
    return {"on_main": on_main_sha, "off_main": off_main_sha}


def test_help_exits_0(capsys):
    rc = main(["--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Usage: check-shipped-on-main.sh" in out
    assert "Exit codes:" in out


def test_unknown_option_exits_1(capsys):
    rc = main(["--bogus"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Unknown option: --bogus" in err


def test_not_inside_git_repo_exits_2(tmp_path, monkeypatch, capsys):
    empty_dir = tmp_path / "not_a_repo"
    empty_dir.mkdir()
    monkeypatch.chdir(empty_dir)
    rc = main(["HEAD"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not inside a git repository" in err


def test_no_commits_specified_exits_1(repo_with_origin, capsys):
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no commits specified" in err


def test_commit_on_main_verbose(repo_with_origin, capsys):
    sha = repo_with_origin["on_main"]
    rc = main(["--verbose", sha])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"{sha[:8]}: ON_MAIN" in out


def test_commit_not_on_main_verbose(repo_with_origin, capsys):
    sha = repo_with_origin["off_main"]
    rc = main(["--verbose", sha])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"{sha[:8]}: NOT_ON_MAIN" in out
    assert "ago)" in out


def test_commit_on_main_non_verbose_silent(repo_with_origin, capsys):
    sha = repo_with_origin["on_main"]
    rc = main([sha])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


def test_mixed_commits_any_off_main_exits_1(repo_with_origin, capsys):
    rc = main(["--verbose", repo_with_origin["on_main"], repo_with_origin["off_main"]])
    out = capsys.readouterr().out
    assert rc == 1
    assert "ON_MAIN" in out
    assert "NOT_ON_MAIN" in out


def test_unresolvable_ref_skips_and_exits_1(repo_with_origin, capsys):
    rc = main(["--verbose", "not-a-real-ref"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot resolve 'not-a-real-ref'" in err
