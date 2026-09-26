
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.meta_repo_identity import (
    MetaRepoResolutionError,
    is_meta_repo,
)
from coordinator_core.testing import symlink_capability


# --- explicit git_root, CLAUDE_HOME set (test-sandbox shape) --------------


def test_true_when_git_root_equals_claude_home_meta_root(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_meta = fake_home / ".claude"
    fake_meta.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.delenv("HOME", raising=False)

    assert is_meta_repo(str(fake_meta)) is True


def test_false_when_git_root_is_a_sibling_repo(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_meta = fake_home / ".claude"
    fake_meta.mkdir(parents=True)
    fake_sibling = tmp_path / "some-other-repo"
    fake_sibling.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.delenv("HOME", raising=False)

    assert is_meta_repo(str(fake_sibling)) is False


@symlink_capability.requires_symlink_capability
def test_canonicalizes_symlinked_git_root(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_meta = fake_home / ".claude"
    fake_meta.mkdir(parents=True)
    symlinked = tmp_path / "meta-symlink"
    symlinked.symlink_to(fake_meta)
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.delenv("HOME", raising=False)

    assert is_meta_repo(str(symlinked)) is True


# --- CLAUDE_HOME precedence -------------------------------------------------


def test_empty_claude_home_raises_resolution_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_HOME", "")
    with pytest.raises(MetaRepoResolutionError):
        is_meta_repo(str(tmp_path))


def test_falls_back_to_home_when_claude_home_unset(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_meta = fake_home / ".claude"
    fake_meta.mkdir(parents=True)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))

    assert is_meta_repo(str(fake_meta)) is True


def test_raises_when_cwd_is_not_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(MetaRepoResolutionError):
        is_meta_repo(None)


def test_raises_when_git_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))

    def _boom(*args, **kwargs):
        raise OSError("git: command not found")

    monkeypatch.setattr(subprocess, "run", _boom)
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    monkeypatch.chdir(not_a_repo)
    with pytest.raises(MetaRepoResolutionError):
        is_meta_repo(None)


def test_false_when_neither_side_exists_on_disk(tmp_path, monkeypatch):
    fake_home = tmp_path / "home-does-not-exist"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.delenv("HOME", raising=False)
    ghost_git_root = str(tmp_path / "ghost-repo")

    assert is_meta_repo(ghost_git_root) is False
