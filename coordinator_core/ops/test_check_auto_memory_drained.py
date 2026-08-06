"""
Tests for coordinator_core.ops.check_auto_memory_drained.

Spec backlink: example-doctrine-repo
  docs/plans/2026-07-30-boot-doctrine-cut-and-refill-gate.md § C13, AC15.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.check_auto_memory_drained import (
    _slugify_repo_root,
    main,
)


def _run(*extra_args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = main(list(extra_args))
    return exit_code, out.getvalue(), err.getvalue()


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)


def test_slugify_replaces_path_separators_with_dashes() -> None:
    assert _slugify_repo_root("/home/example/repos/claude-klabauter") == (
        "-home-example-repos-claude-klabauter"
    )
    assert _slugify_repo_root("/home/example/repos/example-doctrine-repo") == (
        "-home-example-repos-example-doctrine-repo"
    )


def test_slugify_normalizes_backslash_separators() -> None:
    win_style = "example-drive" + chr(92) + "Users" + chr(92) + "example" + chr(92) + "repo"
    assert _slugify_repo_root(win_style) == "example-drive-Users-example-repo"


def test_no_memory_dir_is_a_clean_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, out, err = _run("--root", str(repo))
    assert exit_code == 0
    assert out == ""
    assert err == ""


def test_empty_memory_dir_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, _ = _run("--root", str(repo))
    assert exit_code == 0


def test_index_only_residue_still_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text("# Memory Index\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo))
    assert exit_code == 1
    assert "MEMORY.md" in err


def test_sibling_body_only_residue_still_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ZERO MEANS THE DIRECTORY, NOT THE INDEX -- a drained index with a
    surviving sibling body file must still fail the gate."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    slug = _slugify_repo_root(str(repo))
    memory_dir = home / ".claude" / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "some-fact.md").write_text("---\nname: some-fact\n---\nbody\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo))
    assert exit_code == 1
    assert "some-fact.md" in err


def test_home_union_checks_both_home_and_userprofile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Git-Bash HOME/USERPROFILE divergence must never leave a real
    memory dir unchecked -- residue under either resolved root blocks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    home = tmp_path / "home"
    userprofile = tmp_path / "userprofile"
    slug = _slugify_repo_root(str(repo))
    (userprofile / ".claude" / "projects" / slug / "memory").mkdir(parents=True)
    (userprofile / ".claude" / "projects" / slug / "memory" / "stray.md").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)

    exit_code, _, err = _run("--root", str(repo))
    assert exit_code == 1
    assert "stray.md" in err


def test_unresolvable_root_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    exit_code, out, err = _run()
    assert exit_code == 0
