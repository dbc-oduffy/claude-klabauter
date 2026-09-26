from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.sync_main import main
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _make_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, **no_console_passthrough_kwargs())

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, **no_console_passthrough_kwargs())
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
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
    _git_init = subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_passthrough_kwargs())
    monkeypatch.chdir(tmp_path)
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 0
    assert "origin/main not reachable" in err


def test_on_main_ff_pull_succeeds(tmp_path, capsys, monkeypatch):
    origin, clone = _make_origin_and_clone(tmp_path)

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, **no_console_passthrough_kwargs())
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
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, **no_console_passthrough_kwargs())
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

    _git(clone, "checkout", "-q", "main")
    for i in range(51):
        _git(clone, "commit", "-q", "--allow-empty", "-m", f"commit {i}")
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

    _git(clone, "checkout", "-q", "main")
    for i in range(51):
        _git(clone, "commit", "-q", "--allow-empty", "-m", f"commit {i}")
    _git(clone, "checkout", "-q", "work/testmachine/2026-01-01")
    _git(clone, "push", "-q", "origin", "main")

    monkeypatch.chdir(clone)
    rc = main([])
    err = capsys.readouterr().err
    assert rc == 0
    assert "WARNING" in err
    assert "commits behind main" in err
