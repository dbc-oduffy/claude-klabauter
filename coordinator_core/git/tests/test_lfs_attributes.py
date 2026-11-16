"""lfs_attributes: comment-stripped scan, nested/info sources, zero spawns."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git.lfs_attributes import (
    attributes_text_declares_lfs,
    clone_declares_lfs,
)
from pathlib import Path

LFS = "*.bin filter=lfs diff=lfs merge=lfs -text\n"


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    return root


def test_comment_only_mention_is_false() -> None:
    assert attributes_text_declares_lfs("# filter=lfs is off\n*.txt text\n") is False


def test_declaration_with_trailing_comment_is_true() -> None:
    assert attributes_text_declares_lfs(LFS.rstrip() + " # note\n") is True


def test_root_gitattributes_true(repo) -> None:
    (repo / ".gitattributes").write_text(LFS)
    assert clone_declares_lfs(repo) is True


def test_comment_only_root_false(repo) -> None:
    (repo / ".gitattributes").write_text("# filter=lfs\n")
    assert clone_declares_lfs(repo) is False


def test_info_attributes_true(repo) -> None:
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text(LFS)
    assert clone_declares_lfs(repo) is True


def test_nested_tracked_true(repo) -> None:
    (repo / "sub").mkdir()
    (repo / "sub" / ".gitattributes").write_text(LFS)
    _git(repo, "add", "sub/.gitattributes")
    assert clone_declares_lfs(repo) is True


def test_nested_untracked_false(repo) -> None:
    (repo / "sub").mkdir()
    (repo / "sub" / ".gitattributes").write_text(LFS)
    assert clone_declares_lfs(repo) is False


def test_not_a_repo_false(tmp_path: Path) -> None:
    assert clone_declares_lfs(tmp_path) is False


def test_zero_spawns(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "sub").mkdir()
    (repo / "sub" / ".gitattributes").write_text(LFS)
    _git(repo, "add", "sub/.gitattributes")

    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    assert clone_declares_lfs(repo) is True
