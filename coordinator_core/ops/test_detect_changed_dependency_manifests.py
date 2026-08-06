"""Tests for coordinator_core.ops.detect_changed_dependency_manifests.

All git exercise runs against a throwaway repo created fresh under
`tmp_path` per test — NEVER against this working repo. See module
docstring for the op-key/contract this covers:
`dependency.detect_changed_manifests`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.detect_changed_dependency_manifests import (
    _handler,
    _is_manifest_path,
    changed_manifest_files_since,
    detect,
    tracked_manifest_files,
)


def _git(*args: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=env,
    )


@pytest.fixture(autouse=True)
def _isolate_global_git_config(tmp_path, monkeypatch):
    """Isolate from the ambient dev machine's global git config, mirroring
    the same fixture in test_agent_worktree_sweep.py — this module's own
    subprocess calls inherit os.environ, so the override must be process-
    wide, not a one-off subprocess env=.
    """
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)


def _commit_file(root: Path, rel_path: str, content: str, *, when: str | None = None) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git("add", rel_path, cwd=root)
    env = None
    if when is not None:
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    _git("commit", "-q", "-m", f"add {rel_path}", cwd=root, env=env)


def test_is_manifest_path_matches_basename_across_subdirs():
    assert _is_manifest_path("package.json")
    assert _is_manifest_path("backend/package.json")
    assert _is_manifest_path("requirements-dev.txt")
    assert not _is_manifest_path("README.md")
    assert not _is_manifest_path("src/main.py")


def test_no_manifests_tracked_returns_false_false_empty(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    _commit_file(root, "README.md", "hello\n")

    result = detect(root)

    assert result == {"has_manifests": False, "changed_recently": False, "changed_files": []}


def test_manifests_tracked_but_not_recently_changed(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    old_date = "2020-01-01T00:00:00"
    _commit_file(root, "package.json", "{}\n", when=old_date)

    result = detect(root, since_days=14)

    assert result["has_manifests"] is True
    assert result["changed_recently"] is False
    assert result["changed_files"] == []


def test_manifest_changed_recently_is_detected_and_sorted_unique(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    _commit_file(root, "README.md", "hello\n")
    _commit_file(root, "package.json", "{}\n")
    _commit_file(root, "backend/requirements.txt", "flask\n")
    # A second edit to the same manifest must not duplicate the entry.
    _commit_file(root, "package.json", '{"name": "x"}\n')

    result = detect(root, since_days=14)

    assert result["has_manifests"] is True
    assert result["changed_recently"] is True
    assert result["changed_files"] == sorted(set(result["changed_files"]))
    assert "package.json" in result["changed_files"]
    assert "backend/requirements.txt" in result["changed_files"]
    assert result["changed_files"].count("package.json") == 1


def test_since_days_window_excludes_old_but_includes_new(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    _commit_file(root, "Cargo.toml", "[package]\n", when="2020-01-01T00:00:00")
    _commit_file(root, "go.mod", "module x\n")

    result = detect(root, since_days=14)

    assert result["has_manifests"] is True
    assert result["changed_recently"] is True
    assert "go.mod" in result["changed_files"]
    assert "Cargo.toml" not in result["changed_files"]


def test_not_a_git_repo_returns_false_false_empty(tmp_path):
    root = tmp_path / "not-a-repo"
    root.mkdir()
    (root / "package.json").write_text("{}\n")

    result = detect(root)

    assert result == {"has_manifests": False, "changed_recently": False, "changed_files": []}


def test_double_invocation_is_idempotent(tmp_path):
    """AC7: a second invocation with identical inputs is a safe no-op that
    returns a byte-identical result — no side effects to accumulate since
    this op never writes.
    """
    root = tmp_path / "repo"
    _init_repo(root)
    _commit_file(root, "package.json", "{}\n")
    _commit_file(root, "yarn.lock", "\n")

    first = detect(root, since_days=14)
    second = detect(root, since_days=14)

    assert first == second


def test_tracked_manifest_files_helper_lists_only_manifests(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    _commit_file(root, "README.md", "hello\n")
    _commit_file(root, "pyproject.toml", "[project]\n")

    files = tracked_manifest_files(root)

    assert files == ["pyproject.toml"]


def test_changed_manifest_files_since_empty_when_stage_one_would_be_empty(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    _commit_file(root, "README.md", "hello\n")

    assert changed_manifest_files_since(root, 14) == []


@pytest.mark.parametrize("since_days_param", [14, "14", None])
def test_handler_resolves_repo_root_from_params_and_coerces_since_days(tmp_path, since_days_param):
    root = tmp_path / "repo"
    _init_repo(root)
    _commit_file(root, "package.json", "{}\n")

    params = {"repo_root": str(root)}
    if since_days_param is not None:
        params["since_days"] = since_days_param

    # Review: code-reviewer — Finding 1. Handler is a plain sync `def`
    # (engine auto-offloads via asyncio.to_thread), called directly.
    result = _handler(params, repo_root=None)

    assert result["has_manifests"] is True
    assert result["changed_recently"] is True


def test_handler_falls_back_to_dispatch_repo_root_when_params_omit_it(tmp_path):
    root = tmp_path / "repo"
    _init_repo(root)
    _commit_file(root, "Gemfile", "source 'https://rubygems.org'\n")

    result = _handler({}, repo_root=root)

    assert result["has_manifests"] is True
