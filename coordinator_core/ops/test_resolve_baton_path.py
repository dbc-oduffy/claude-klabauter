"""Tests for coordinator_core.ops.resolve_baton_path (op baton.resolve_path_and_repo).

Wave-3 settlement B5 coverage: native path resolution, git-computed relative path
(no manual prefix strip), outside-any-repo structured error, and the CC-4
double-invocation idempotency proof. Git runs only against tmp_path throwaway
repos — never the working repo.
"""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.resolve_baton_path import _resolve_baton_path_and_repo


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), check=True)
    return path


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path / "repo")


def _rev_parse_toplevel(cwd):
    return subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.rstrip("\n")


def test_baton_in_repo_subdir(repo):
    baton_dir = repo / "state" / "handoffs"
    baton_dir.mkdir(parents=True)
    baton = baton_dir / "2026-07-22_baton.md"
    baton.write_text("baton\n", encoding="utf-8")

    result = _resolve_baton_path_and_repo({"baton_path": str(baton)})

    assert "error" not in result
    assert result["abs_path"] == str(baton.resolve())
    # repo_root comes verbatim from git — compare against git's own answer so the
    # assertion is macOS-/private-symlink- and Windows-drive-form agnostic.
    assert result["repo_root"] == _rev_parse_toplevel(baton_dir)
    assert result["git_relative_path"] == "state/handoffs/2026-07-22_baton.md"


def test_baton_at_repo_root_empty_prefix(repo):
    baton = repo / "baton.md"
    baton.write_text("baton\n", encoding="utf-8")

    result = _resolve_baton_path_and_repo({"baton_path": str(baton)})

    assert "error" not in result
    assert result["git_relative_path"] == "baton.md"


def test_relative_path_forms_resolve(repo, monkeypatch):
    baton_dir = repo / "docs"
    baton_dir.mkdir()
    baton = baton_dir / "note.md"
    baton.write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    result = _resolve_baton_path_and_repo({"baton_path": "docs/note.md"})

    assert "error" not in result
    assert result["abs_path"] == str(baton.resolve())
    assert result["git_relative_path"] == "docs/note.md"


def test_tilde_expansion_applied(repo, monkeypatch):
    monkeypatch.setenv("HOME", str(repo))
    monkeypatch.setenv("USERPROFILE", str(repo))
    baton = repo / "b.md"
    baton.write_text("x\n", encoding="utf-8")

    result = _resolve_baton_path_and_repo({"baton_path": "~/b.md"})

    assert "error" not in result
    assert result["abs_path"] == str(baton.resolve())


def test_outside_any_repo_structured_error(tmp_path):
    loose = tmp_path / "not-a-repo" / "baton.md"
    loose.parent.mkdir(parents=True)
    loose.write_text("x\n", encoding="utf-8")

    result = _resolve_baton_path_and_repo({"baton_path": str(loose)})

    assert set(result.keys()) == {"error"}
    assert "not inside any git repository" in result["error"]


def test_missing_parent_dir_structured_error(tmp_path):
    ghost = tmp_path / "no-such-dir" / "baton.md"

    result = _resolve_baton_path_and_repo({"baton_path": str(ghost)})

    assert set(result.keys()) == {"error"}


def test_missing_param_structured_error():
    assert "error" in _resolve_baton_path_and_repo({})
    assert "error" in _resolve_baton_path_and_repo({"baton_path": ""})
    assert "error" in _resolve_baton_path_and_repo({"baton_path": 42})


def test_double_invocation_identical_result(repo):
    """CC-4 idempotency proof: pure read — second call with identical inputs is a
    safe no-op returning the identical documented shape."""
    baton_dir = repo / "state"
    baton_dir.mkdir()
    baton = baton_dir / "baton.md"
    baton.write_text("baton\n", encoding="utf-8")
    params = {"baton_path": str(baton)}

    first = _resolve_baton_path_and_repo(params)
    second = _resolve_baton_path_and_repo(params)

    assert "error" not in first
    assert first == second


def test_registered_under_op_key():
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("baton.resolve_path_and_repo") is _resolve_baton_path_and_repo
