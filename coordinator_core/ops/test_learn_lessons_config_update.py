"""Tests for coordinator_core.ops.learn_lessons_config_update."""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.learn_lessons_config_update import (
    _norm,
    _slugify,
    main,
)
from coordinator_core.testing.fake_machine_local import write_fake_machine_local


def test_meta_repo_cwd_is_silent_noop(tmp_path, monkeypatch, capsys):
    """cwd equal to CLAUDE_HOME: silent no-op, exit 0."""
    home_parent = tmp_path / "home"
    home_parent.mkdir()
    claude_home = home_parent / ".claude"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home_parent))
    monkeypatch.chdir(claude_home)

    rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_registered_repo_is_silent_noop(tmp_path, monkeypatch, capsys):
    """cwd matches a machine-local repos.* entry -> silent no-op, exit 0."""
    home_parent = tmp_path / "home"
    claude_home = home_parent / ".claude"
    claude_home.mkdir(parents=True)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home_parent))
    monkeypatch.chdir(repo)

    python_body = (
        "import sys, json\n"
        "argv = sys.argv[1:]\n"
        "if argv[:3] == ['dump', '--prefix', 'repos']:\n"
        f"    print(json.dumps({{'repos.myrepo': {str(repo)!r}}}))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    bin_dir = write_fake_machine_local(tmp_path, python_body).parent
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_registered_repo_among_multiple_dump_entries_is_silent_noop(tmp_path, monkeypatch, capsys):
    """Multi-item dump: cwd matches the SECOND of several repos.* entries -> still a
    no-op. A single-entry dump fixture would pass identically whether the lookup
    scans the whole snapshot or short-circuits on the first key, masking a
    batching regression that only shows up with >1 registered repo (the same gap
    that let a batched `_own_frozen_diff_shas` ship wrong on 2026-08-19)."""
    home_parent = tmp_path / "home"
    claude_home = home_parent / ".claude"
    claude_home.mkdir(parents=True)
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    repo = tmp_path / "myrepo"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home_parent))
    monkeypatch.chdir(repo)

    python_body = (
        "import sys, json\n"
        "argv = sys.argv[1:]\n"
        "if argv[:3] == ['dump', '--prefix', 'repos']:\n"
        "    print(json.dumps({"
        f"'repos.other': {str(other_repo)!r}, "
        f"'repos.myrepo': {str(repo)!r}"
        "}))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    bin_dir = write_fake_machine_local(tmp_path, python_body).parent
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_unregistered_repo_prints_hint_and_exits_zero(tmp_path, monkeypatch, capsys):
    """cwd neither registered nor equal to CLAUDE_HOME: advisory hint on stderr, exit 0."""
    home_parent = tmp_path / "home"
    claude_home = home_parent / ".claude"
    claude_home.mkdir(parents=True)
    repo = tmp_path / "Some-Repo!"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home_parent))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir-for-test")

    rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not a registered learn-lessons root" in captured.err
    assert "machine-local set repos.some_repo " in captured.err


def test_missing_machine_local_falls_through_to_hint(tmp_path, monkeypatch, capsys):
    """machine-local absent entirely -> fail-open to the advisory hint, exit 0."""
    home_parent = tmp_path / "home"
    claude_home = home_parent / ".claude"
    claude_home.mkdir(parents=True)
    repo = tmp_path / "orphan"
    repo.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home_parent))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir-for-test")

    rc = main([])

    assert rc == 0
    captured = capsys.readouterr()
    assert "not a registered learn-lessons root" in captured.err


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MyRepo", "myrepo"),
        ("Some-Repo!", "some_repo"),
        ("__leading_trailing__", "leading_trailing"),
        ("already_clean", "already_clean"),
    ],
)
def test_slugify(raw, expected):
    assert _slugify(raw) == expected


def test_norm_resolves_existing_dir(tmp_path):
    assert _norm(str(tmp_path)) == os.path.realpath(str(tmp_path))


def test_norm_nonexistent_path_returns_input_or_realpath():
    # realpath() on a nonexistent path still returns a normalized absolute path
    # (it doesn't require the path to exist) -- assert it doesn't raise.
    result = _norm("/definitely/does/not/exist/path")
    assert isinstance(result, str)
